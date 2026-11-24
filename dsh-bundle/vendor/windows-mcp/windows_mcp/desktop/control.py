"""Desktop ownership; hooks fail open on stale heartbeats without waiting for locks."""

from __future__ import annotations

import ctypes
import logging
import math
import os
import queue
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

from windows_mcp.desktop.control_context import (
    current_token,
    current_steps,
    current_request_closed,
    get_step_count,
)
from windows_mcp.desktop.control_ledger import InputLedger
from windows_mcp.desktop import control_hooks
from windows_mcp.desktop.control_worker import run_control_worker
from windows_mcp.desktop.control_win32 import (
    _MouseHookData as _MouseHookData,  # Keep hook test helpers on this module.
    _KeyHookData as _KeyHookData,
    _user32,
    _key,
    _interactive_desktop,
    run_input_monitor,
)

# The hand leaving the mouse right after clicking Allow must not take the desktop back.
_CONSENT_GRACE_SECONDS = 1.0


def describe_failure(exc: BaseException) -> str:
    """Return ``Type: message`` for the innermost cause of *exc*."""
    # Must never raise: it runs inside fail-open handlers.
    try:
        seen: set[int] = {id(exc)}
        root = exc
        while root.__cause__ is not None and id(root.__cause__) not in seen:
            root = root.__cause__
            seen.add(id(root))
        name = type(root).__name__
        try:
            text = str(root).strip()
        except Exception:
            text = ""
        return f"{name}: {text}" if text else name
    except Exception:
        return "unknown failure"


class ControlBlocked(RuntimeError):
    def __init__(self, code: str, status: dict):
        self.code = code
        self.status = status
        super().__init__(f"{code}: {status['state']}")


class ControlCoordinator:
    def __init__(self, mouse_takeover_units: int = 500, mouse_takeover_pixels: int = 500):
        self.mouse_takeover_units = mouse_takeover_units
        self.mouse_takeover_pixels = mouse_takeover_pixels  # Legacy config; no pixel fallback.
        # Set once by the server lifespan before any tools or hooks start.
        self.enabled = True
        self.idle_timeout_seconds = 10.0
        # AI 侧空闲多久自动交还控制权。和 idle_timeout_seconds 是反方向的：
        # 那个管「用户空闲多久后 AI 才被允许接管」，这个管「AI 多久没动作就
        # 该放手」。不设的话，调用方忘了 release()，叠加层会一直挂在屏幕上。
        # 默认 120 秒：足够覆盖一次慢工具调用，又不至于让用户盯着蓝光干等。
        self.agent_idle_release_seconds = 120.0
        self._agent_last_active = 0.0
        # 一轮工具调用结束后，叠加层还要留多久再撤（秒）。
        #
        # 为什么需要这个小宽限：一次工具调用就是"AI 用了一下电脑"，用完了就该
        # 把屏幕还回去，不该让用户盯着持续发光的屏幕猜发生了什么。但也不能
        # `end_call` 里立刻撤——智能体常态是连着调 `Input` → `Screen` → `Input`，
        # 间隔几十毫秒。每次都"关掉再打开"会让四周蓝光闪成一片，比一直亮着更难受。
        #
        # 所以这里只记一个"这一轮结束了"的时刻，宽限期过了还没等到新的
        # `begin_call` 才真正撤。默认 2 秒：比人手感知一次"闪"的阈值长得多，
        # 又远小于下面那个 120 秒兜底——用户不用再等两分钟。
        self.glow_release_grace_seconds = 2.0
        # 允许部署时用环境变量覆盖，也是这次修复做真假对照实验的唯一开关：
        # 把宽限期配成 120 就等价于修复前的行为（工具跑完光一直挂着），
        # 方便在同一份代码上对比"改前 / 改后"的抓屏结果。
        #
        # 为什么不走 `~/.windows-mcp/config.toml`：那个读取路径在 `__main__.py`
        # 和 `infrastructure/config.py` 里，正有别人在改；为了一个秒级参数去动
        # 那两处，冲突成本远大于收益。这里只读一个明确前缀的环境变量，
        # 非法值或没设置都落回默认值，不影响服务启动。
        _grace_env = os.environ.get("WINDOWS_MCP_GLOW_GRACE_SECONDS")
        if _grace_env:
            try:
                _grace_value = float(_grace_env)
            except ValueError:
                logger.warning(
                    "WINDOWS_MCP_GLOW_GRACE_SECONDS=%r 不是数字，沿用默认 %s 秒",
                    _grace_env,
                    self.glow_release_grace_seconds,
                )
            else:
                if math.isfinite(_grace_value) and _grace_value >= 0:
                    self.glow_release_grace_seconds = _grace_value
                else:
                    logger.warning(
                        "WINDOWS_MCP_GLOW_GRACE_SECONDS=%r 必须是有限的非负数，沿用默认 %s 秒",
                        _grace_env,
                        self.glow_release_grace_seconds,
                    )
        # (generation, 结束时刻)；generation 用来分辨"这是不是同一轮"。
        # 用户中途接管、或宽限期结束后已经撤过，都必须让旧记录失效，
        # 否则下一次 `begin_call` 会被一条过期时间戳立刻打回 ready。
        self._glow_release_at: tuple[int, float] | None = None
        self.call_lease_timeout_seconds = 30.0
        self.policy = "relaxed"
        self.consent_countdown_seconds = 10
        self._lock = threading.Lock()
        # Granted by the consent prompt; revoked whenever the person takes the desktop.
        self._consented = False
        # Refused, ignored while working, or took the desktop back: do not ask again until idle.
        self._declined = False
        self._consent_started = 0.0
        # Physical input stamped before this moment is not user activity: inf
        # while the prompt is open, then the moment the person allowed control.
        self._input_floor = 0.0
        self._state = "unavailable"
        self._generation = 0
        self._active_calls = 0
        self._active_call_since: float | None = None
        self._last_user = 0.0
        self._last_physical_event = 0.0  # Written before hook events enter the queue.
        self._last_point: tuple[int, int] | None = None  # Cursor baseline for activity.
        self._last_move = 0.0
        self._motion_since = 0.0
        self._raw_distance = 0.0
        self._raw_device: int | None = None
        self._listeners: list[Callable[[dict], None]] = []
        self.input_ledger = InputLedger()
        self._health_probe: Callable[[], bool] | None = None
        # `set_health_probe` 传入的原始探针，语义只有「叠加层是不是活着」。
        # `_health_probe` 是包了一层上下文的门禁版本（见 `_indicator_health_gate`）。
        # 两者要分开：截图恢复等场景必须按原始语义判断，不能因为"还没 armed"
        # 就把"叠加层已经死了"当成健康。
        self._health_probe_raw: Callable[[], bool] | None = None
        self._begin_notification = threading.local()
        self._visual_armed = False
        self._visual_paused = False
        self._visual_event_stamp = 0.0
        # 叠加层没画出来的痕迹。为什么需要记账：
        #
        # `arm_visible()` 原先失败时是静默 `return`，外面完全看不见。于是「叠加层
        # 没显示」这件事只能从「Input 第一步就被 CONTROL_PREEMPTED」这个间接症状
        # 反推，而且 executed_steps 是 0，看着像状态机自己拒了自己。有了这两个
        # 字段，`ControlStatus` 能直接回答「arm 失败过几次、最后一次为什么」。
        self._arm_failures = 0
        self._arm_last_error: str | None = None
        # `begin_call` 兜底时发现「拿到租约了但叠加层没画出来」的标记。
        # 和 `_arm_failures` 分开：那个记 arm_visible 本身的失败，这个记
        # 「已经放行、只是显示降级了」，两者不是同一件事，混在一起会把
        # 正常的一次成功调用也算成失败。
        self._visual_degraded = False
        self._visual_degrade_warned = False
        self._events: queue.Queue[tuple] = queue.Queue(maxsize=4096)
        self._stop = threading.Event()
        self._release_requested = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._startup_error: Exception | None = None
        self._thread_beat = 0.0
        self._suppress = False  # Atomic snapshot read by the hook callbacks.
        self._deadline = 0.0
        self._emergency = False
        # Why the last fail-open happened, reported while the state is unavailable.
        self._fail_reason: str | None = None
        self._fast_takeover = False
        self._fast_pending = False
        self._rotating = False
        self._pressed: set[int] = set()
        self._mouse_down: set[int] = set()
        self._delivered_keys: set[int] = set()
        self._delivered_mouse: set[int] = set()
        self._quarantine: set[int] = set()
        self._mouse_hook = None
        self._key_hook = None
        self._mouse_callback = None
        self._key_callback = None
        self._window_callback = None
        self._rotate = False
        self._fault_token = object()
        self._rehook_completed = None

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        with self._lock:
            self._listeners.append(callback)

    def set_health_probe(self, probe: Callable[[], bool]) -> None:
        """Require the visual indicator to stay alive during AI control."""
        # 保留原始探针，再把一个带条件的门禁装到 `_health_probe` 上：
        # worker 每 0.25 秒调一次它，失败就 `_fail_open()` → 整个桌面控制 unavailable。
        #
        # 为什么需要这个门（而不是直接暴露原始探针）：探针的语义应该只是
        # 「叠加层是不是活着」，而不是「工具还能不能干活」。当 AI 根本没在压制
        # 物理输入（`_visual_armed` False）时，叠加层死没死不影响任何人的操作——
        # 此时它不健康不该把整台机器判成 unavailable。
        #
        # 反过来，一旦真的 armed（正在吞物理输入），叠加层就必须活着：用户需要
        # 看得见「AI 在控」，否则他的键鼠被吞却毫无提示。那种情况门禁照旧放行
        # 不健康，由 worker 走 fail_open 把输入还回去。安全语义一点没松。
        self._health_probe_raw = probe
        self._health_probe = self._indicator_health_gate(probe)

    def _indicator_health_gate(self, probe: Callable[[], bool]) -> Callable[[], bool]:
        """Wrap the indicator probe so it only vetoes while it is load-bearing."""

        def gated() -> bool:
            # 未 armed ⇒ 没有压制，也就没有「看不见的 AI 在吞输入」这回事。
            # 读两个 bool 字段不需要加锁：最坏情况是读到上一拍的快照，而 next tick
            # 会立刻纠正；在这里抢 `self._lock` 反而可能和 begin_call 互等。
            if not (self._visual_armed or self._suppress):
                return True
            return probe()

        return gated

    def visible_ack_required(self) -> bool:
        """Only a new MCP lease must wait for its visual acknowledgement."""
        return bool(getattr(self._begin_notification, "active", False))

    def _arm_block_reason_locked(self, generation: int) -> str | None:
        """Cannot arm: say why, in words a status reader can act on.

        语义上这里分两族，必须分开报告：
          - 良性竞态（`state_not_ai` / `stale_generation` / `visual_paused` /
            `stopping`）：这次 arm 请求本身过期或时机不对。用户抢占、截图暂停、
            释放控制权都会产生它，属于正常工作的一部分，**不计数**——否则
            `arm_failures` 在完全健康的机器上也会涨，这个字段就没用了。
          - 真故障（按键还按着、刚有物理事件、正在故障恢复）：状态是 ai 却又
            确实不能压掉物理输入。这些才计数，因为它们解释了"为什么这一步没挂上"。
        """
        if self._state != "ai":
            return f"state_not_ai:{self._state}"
        if self._generation != generation:
            return f"stale_generation:{generation}->{self._generation}"
        if self._visual_paused:
            return "visual_paused"
        if self._stop.is_set():
            return "stopping"
        if self._last_physical_event != self._visual_event_stamp:
            return "physical_event_since_lease"
        if self._pressed:
            return "physical_keys_held"
        if self._mouse_down:
            return "physical_buttons_held"
        if self._fast_takeover:
            return "fast_takeover"
        if self._fast_pending:
            return "fast_pending"
        if self._emergency:
            return "emergency"
        return None

    @staticmethod
    def _arm_block_is_benign(reason: str) -> bool:
        """上面那一族「正常竞态」，不该计入 `arm_failures`。"""
        return reason.startswith(
            ("state_not_ai:", "stale_generation:", "visual_paused", "stopping")
        )

    def _record_arm_failure_locked(self, reason: str) -> None:
        """记一次 arm 失败。调用方必须已持有 `self._lock`。

        为什么只记不清零：这是一个诊断计数器。清零会让「跑了一整天、偶尔失败
        十几次」这种真正的问题在下次查询时看起来是 0。要看当前租约是否干净，
        看 `visual_armed`；要看历史上出过什么事，看这个数。
        """
        self._arm_failures += 1
        self._arm_last_error = reason

    def arm_visible(self, generation: int) -> None:
        """Suppress physical input only after the AI indicator is visible."""
        with self._lock:
            reason = self._arm_block_reason_locked(generation)
            if reason is not None:
                # 原先这里是静默 return，外面完全不知道 arm 没成。
                # 记账之后 ControlStatus 里能直接看到失败次数与最后一次原因。
                if self._arm_block_is_benign(reason):
                    logger.debug("AI control indicator arm skipped: %s", reason)
                else:
                    self._record_arm_failure_locked(reason)
                return
            self.input_ledger.enable()
            self._visual_armed = True
            self._deadline = time.monotonic() + 1.0
            self._suppress = True
            if self._stop.is_set() or self._last_physical_event != self._visual_event_stamp:
                self._suppress = False
                self._visual_armed = False
                self.input_ledger.block_new()
                self._record_arm_failure_locked("physical_event_after_arm")
            else:
                # 成功挂上了，之前那次「叠加层没显示」的降级就结束了。
                self._visual_degraded = False

    def pause_for_capture(self) -> int | None:
        """Release physical input before an AI indicator is hidden for capture."""
        with self._lock:
            # 每次 AI 动作都刷新一次活动时间。下面 _agent_idle_release 用它判断
            # 智能体是不是已经不再干活了。
            self._agent_last_active = time.monotonic()
            if self._state != "ai" or not self._visual_armed:
                return None
            self._suppress = False
            self._visual_armed = False
            self._visual_paused = True
            self._visual_event_stamp = self._last_physical_event
            self.input_ledger.block_new()
            return self._generation

    def _agent_idle_release(self, now: float) -> bool:
        """智能体长时间没有动作就自动交还控制权。

        为什么需要：拿到控制权后必须由调用方显式 release()，叠加层才会撤。
        `idle_timeout_seconds` 管的是反方向（用户空闲多久后 AI 才被允许接管），
        对「AI 拿到控制权后就不管了」没有任何约束。

        实测踩过：智能体跑完一轮对话没调 release，四周蓝光和顶部提示面板
        在屏幕上挂了七十多分钟，用户以为程序卡死。

        Returns True 表示这一轮刚触发交还，需要通知界面。
        """
        if self._state != "ai" or self._agent_last_active <= 0:
            return False
        if now - self._agent_last_active < self.agent_idle_release_seconds:
            return False
        changed = self._set_locked("ready")
        if changed:
            self._agent_last_active = 0.0
        return changed

    def schedule_glow_release(self) -> None:
        """记下「这一轮结束了」的时刻；宽限期过了还没新调用就撤掉叠加层。

        为什么要有这一步：`end_call` 原先只把 `_active_calls` 减一，完全没碰
        叠加层。于是每次工具调用结束后，四周蓝光和顶部「AI 正在控制这台电脑」
        面板会一直挂着，直到 `agent_idle_release_seconds`（120 秒）兜底生效，
        或者调用方显式 `release()`。用户看着一个持续发光的屏幕，只会以为程序
        卡死了。一次工具调用就是"AI 用了一下电脑"，用完就该还回去。

        为什么不是立刻撤：智能体常态是连着调 `Input` → `Screen` → `Input`，
        间隔几十毫秒。每次 `end_call` 都立刻关、下一条 `begin_call` 再开，
        四周蓝光会闪成一片，比一直亮着更难受。宽限期把这段连发当成一轮。

        为什么记 generation：用户中途物理接管、或者宽限期结束后已经撤过，
        这条时间戳就必须失效。否则一条过期记录会让下一次 `begin_call` 之后
        的新租约被立刻打回 ready。

        也是在 `_lock` 外面被 worker 调用的，和 `_agent_idle_release` 一致。
        """
        if self.glow_release_grace_seconds <= 0:
            return  # 配成 0 或负数等于关掉这个机制，保留旧的 120 秒兜底行为。
        with self._lock:
            # 工具还在跑就不能记：`Screen` 这类长调用期间撤光是错的。
            if self._active_calls > 0 or self._state != "ai":
                return
            self._glow_release_at = (self._generation, time.monotonic())

    def _glow_release_due(self, now: float) -> bool:
        """宽限期到了就真正交还控制权；返回 True 表示这一轮刚撤。

        只在「同一轮、没在跑、宽限期已过」三个条件同时成立时才撤。任何一条
        不成立都要把记录清掉，不然它会一直挂着，把下一轮的判定带偏。
        """
        with self._lock:
            pending = self._glow_release_at
            if pending is None:
                return False
            generation, ended_at = pending
            if (
                self._active_calls > 0  # 新调用已经接上了，这一轮还没结束。
                or self._state != "ai"  # 已经被用户接管、或已经撤过了。
                or generation != self._generation  # 隔了一轮，时间戳作废。
            ):
                self._glow_release_at = None
                return False
            if now - ended_at < self.glow_release_grace_seconds:
                return False
            self._glow_release_at = None
            return self._set_locked("ready")

    def resume_after_capture(self, generation: int | None, *, restored: bool) -> None:
        """Re-arm only after the indicator is restored and no user event occurred."""
        if generation is None:
            return
        self._agent_last_active = time.monotonic()
        # 这里用**原始**探针，不用带条件的门禁：截图恢复之后必须把叠加层重新
        # 画出来才允许再次压制物理输入。这一条是安全语义，不能因为"反正还没
        # armed"就跳过——正是这次 arm 要把它打开。
        if not restored or (self._health_probe_raw is not None and not self._health_probe_raw()):
            self._fail_open()
            return
        with self._lock:
            if generation == self._generation:
                self._visual_paused = False
        self.arm_visible(generation)

    def _snapshot_locked(self, now: float) -> dict:
        state = (
            "disabled"
            if not self.enabled
            else "unavailable"
            if self._emergency or self._stop.is_set() or self._state == "unavailable"
            else "user"
            if self._fast_takeover
            else "takeover_pending"
            if self._fast_pending and self._state == "ai"
            else self._state
        )
        last_input = max(self._last_user, self._last_physical_event)
        wait = max(0.0, last_input + self.idle_timeout_seconds - now) if state == "user" else 0.0
        return {
            "state": state,
            "generation": self._generation,
            "wait_seconds": wait,
            "enabled": self.enabled,
            "policy": self.policy,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            # 叠加层（四周蓝光 + 顶部提示面板）这一路的健康度，和桌面归属分开看。
            #
            # `visual_armed` 才是「AI 正在压着物理输入」的真凭据；
            # `arm_failures` / `arm_last_error` 回答「这个租约期间为什么没挂上」；
            # `visual_degraded` 表示当前这次调用已经放行，但叠加层没画出来。
            #
            # 为什么要暴露这几项：`arm_visible` 失败以前是静默的，用户只能从
            # 「Input 一步都没走就被 CONTROL_PREEMPTED」反推，而那条错误信息
            # 里 executed_steps 恒为 0，看起来像状态机自己在拒绝自己。
            "visual_armed": self._visual_armed,
            "visual_degraded": self._visual_degraded,
            "arm_failures": self._arm_failures,
            "arm_last_error": self._arm_last_error,
            # 交还节奏也摆出来：`glow_release_grace_seconds` 是一轮调用结束后的
            # 宽限期，`agent_idle_release_seconds` 是长时间没动作的兜底。
            # `glow_release_pending` 为 True 表示"这一轮已经结束、正在等宽限期过"，
            # 是用户看到"光还在但马上要撤"时唯一能解释这件事的字段。
            "glow_release_grace_seconds": self.glow_release_grace_seconds,
            "agent_idle_release_seconds": self.agent_idle_release_seconds,
            "glow_release_pending": self._glow_release_at is not None,
            **(
                {"reason": self._fail_reason}
                if state == "unavailable" and self._fail_reason
                else {}
            ),
        }

    def status(self) -> dict:
        if not self.enabled:
            # A read must not tick ownership or release holds between MCP calls.
            return self._snapshot_locked(time.monotonic())
        if self._emergency:
            self._mark_unavailable()
        now = time.monotonic()
        with self._lock:
            changed = self._set_locked("unavailable") if self._emergency else self._tick_locked(now)
            result = self._snapshot_locked(now)
        if changed:
            self._notify(result)
        return result

    def _notify(self, result: dict) -> None:
        if result["state"] != "ai":
            # Serialize hold cleanup against a new lease, before any listener
            # can wait on a visual effect or an MCP transport.
            with self._lock:
                if result["generation"] != self._generation:
                    return
                try:
                    self.input_ledger.release_all()
                except Exception:
                    self._fail_open()
                    self._set_locked("unavailable")
                    result = self._snapshot_locked(time.monotonic())
        for listener in tuple(self._listeners):
            try:
                listener(result)
            except Exception:
                pass  # A disconnected MCP session must not hold desktop control.

    def _set_locked(self, state: str) -> bool:
        if state == self._state:
            return False
        previous, self._state = self._state, state
        if state != "ai":
            self.input_ledger.block_new()
        if state in ("user", "unavailable"):
            self._consented = False
        if state == "ready":
            self._declined = False
        elif state == "user" and previous == "ai":
            self._declined = True
        self._generation += 1
        # 进入 ai 时**故意**不直接置 `_visual_armed`（探针存在时）。
        #
        # 这条判断是本次 bug 的起因之一，但本身没错：arm 必须等到叠加层真的画出来
        # 才算数，否则会出现「AI 在吞物理输入、屏幕上却什么都没有」——那才是真正
        # 危险的。既然 `__main__` 注册了健康探针，这里就恒为 False，之后由
        # `arm_visible()` 把它翻成 True。
        #
        # 出问题的是下游：`checkpoint()` 把这个字段当成了"允许动手"的许可证。
        # 叠加层画不出来时它一直是 False，第一步就被拒。现在 checkpoint 不再看它，
        # 这条 arm 的时序语义因此保持原样——安全的那一半没有被放宽。
        self._visual_armed = state == "ai" and self._health_probe is None
        self._visual_paused = False
        self._visual_event_stamp = self._last_physical_event
        self._raw_distance = 0.0
        self._raw_device = None
        if state == "ai":
            self._motion_since = time.monotonic()
        if state != "takeover_pending":
            self._fast_pending = False
        self._suppress = self._visual_armed and not self._emergency
        if state != "ai":
            # 离开 ai 就把「叠加层降级」这件事清掉：那是当前租约的属性，
            # 不是进程级的历史。历史失败次数留在 _arm_failures 里。
            self._visual_degraded = False
        if self._stop.is_set():
            self._suppress = False
            self.input_ledger.block_new()
        self._deadline = time.monotonic() + 1.0 if self._suppress else 0.0
        return True

    def _tick_locked(self, now: float) -> bool:
        # Zero means no input has been observed, even with a timeout longer than uptime.
        stamp = self._last_physical_event
        recent = (
            stamp > 0 and stamp >= self._input_floor and now - stamp < self.idle_timeout_seconds
        )
        if self._state == "ready" and recent:
            self._last_user = self._last_physical_event
            return self._set_locked("user")
        if (
            self._state == "user"
            and not self._pressed
            and not self._mouse_down
            and now - max(self._last_user, self._last_physical_event) >= self.idle_timeout_seconds
        ):
            return self._set_locked("ready")
        if (
            self._state == "ai"
            and self._active_calls > 0
            and self._active_call_since is not None
            and self.call_lease_timeout_seconds > 0
            and now - self._active_call_since >= self.call_lease_timeout_seconds
        ):
            # Time-bound the input suppression lease when an in-flight call stalls.
            return self._set_locked("ready")
        # Otherwise AI control ends on release(), a user takeover, or shutdown.
        return False

    def would_ask(self) -> bool:
        """Whether a call while the person uses the desktop should ask instead of failing."""
        return self.consent_countdown_seconds > 0 and not self._declined

    def open_consent(self) -> bool:
        """Start a consent prompt if this call would begin a new AI control period."""
        if self.status()["state"] not in ("ready", "user"):
            return False  # Within a lease, or blocked anyway by begin_call.
        with self._lock:
            if (
                self._consented
                or self._declined
                or self.consent_countdown_seconds <= 0
                or self._state not in ("ready", "user")
            ):
                return False
            # Moving the mouse to the prompt must not count as taking the desktop.
            self._input_floor = math.inf
            self._consent_started = time.monotonic()
            return True

    def close_consent(self, answer: str) -> None:
        """Apply allow, deny, timeout or error; an ignored prompt with input denies."""
        now = time.monotonic()
        changed = False
        with self._lock:
            touched = self._last_physical_event > self._consent_started
            granted = answer == "allow" or (answer == "timeout" and not touched)
            # Input that answered the prompt, or follows a click on Allow within the grace,
            # is not a takeover. Input after an unattended countdown still is.
            if not granted:
                self._input_floor = 0.0
            elif answer == "allow":
                self._input_floor = now + _CONSENT_GRACE_SECONDS
            else:
                self._input_floor = now
            if granted and self._state == "user":
                changed = self._set_locked("ready")  # The person at the desk handed it over.
            elif touched and not granted and self._state == "ready":
                self._last_user = now
                changed = self._set_locked("user")
            self._consented = granted
            self._declined = touched and not granted
            result = self._snapshot_locked(now)
        if changed:
            self._notify(result)

    def begin_call(self, name: str) -> int:
        if self._emergency:
            self._mark_unavailable()
        now = time.monotonic()
        with self._lock:
            changed = self._set_locked("unavailable") if self._emergency else self._tick_locked(now)
            if self._pressed or self._mouse_down:
                raise ControlBlocked("PHYSICAL_INPUT_HELD", self._snapshot_locked(now))
            if (
                self._stop.is_set()
                or self._emergency
                or self._fast_takeover
                or self._fast_pending
                or self._rotating
                or self._visual_paused
                or self._state not in ("ready", "ai")
            ):
                status = self._snapshot_locked(now)
                code = {"user": "USER_CONTROL", "takeover_pending": "TAKEOVER_PENDING"}.get(
                    status["state"], "CONTROL_UNAVAILABLE"
                )
                raise ControlBlocked(code, status)
            try:
                self.input_ledger.enable()  # Any failed release keeps the lease closed.
            except RuntimeError:
                self._fail_open()
                self._set_locked("unavailable")
                raise ControlBlocked("CONTROL_UNAVAILABLE", self._snapshot_locked(now)) from None
            # stop() may run while enable() waits for the input ledger lock.
            if self._stop.is_set():
                self.input_ledger.block_new()
                raise ControlBlocked("CONTROL_UNAVAILABLE", self._snapshot_locked(now))
            changed = self._set_locked("ai") or changed
            self._active_calls += 1
            if self._active_call_since is None:
                self._active_call_since = now
            token = self._generation
            result = self._snapshot_locked(now)
        if changed:
            self._begin_notification.active = True
            try:
                self._notify(result)
            finally:
                self._begin_notification.active = False
        # 兜底：通知链路走完了，叠加层还是没挂上。
        #
        # 为什么需要这一段：`_notify` 同步回调到 `__main__.apply_control_state`，
        # 那里要 `control_overlay.set_active(True, ...)` 等 overlay 线程 ack（最多
        # 3 秒）。ack 超时、overlay 不健康、或 generation 在此期间又变了，都会让
        # 它返回 False，于是 `arm_visible` 从不被调用，`_visual_armed` 一直是 False。
        # 紧接着 `checkpoint()` 第一条就抛 CONTROL_PREEMPTED、executed_steps 为 0，
        # 也就是「租约拿到了，但工具一步都动不了」。
        #
        # 叠加层是给人看的提示，不是「允许 AI 动手」的许可证。它失败了应该降级
        # （记状态、上报 health），而不是把功能一起掐掉。物理输入抢占不依赖这里：
        # `arm_visible` / `_suppress` 依然按老规则工作，只是不再当 checkpoint 的门。
        #
        # 为什么放在 `if changed:` 外面：同一租约里的第 2..n 次调用不会重新通知
        # 叠加层，第一次没画出来时这里也得把降级状态记着，否则 ControlStatus 会
        # 看起来一切正常。
        self._note_visual_degraded()
        return token

    def note_arm_failure(self, reason: str) -> None:
        """记一次叠加层失败；供 `__main__` 在 set_active 返回 False/抛错时调用。

        为什么开这个口子：`arm_visible` 只能看见「状态机这边为什么不能 arm」，
        而 `set_active` 失败（ack 超时、overlay 不健康、generation 过期）发生
        在它之前，那边才是更常见的原因。两边记同一个计数器，ControlStatus 才
        能给出一个完整的回答。
        """
        with self._lock:
            self._record_arm_failure_locked(reason)

    def _note_visual_degraded(self) -> None:
        """记一次「叠加层没显示」，只警告一次，不影响调用继续。"""
        with self._lock:
            if self._visual_armed or self._state != "ai":
                return
            # 租约确实拿到了，只是显示没成功。具体原因由 arm_visible /
            # note_arm_failure 记账，这里只标「已放行但降级」。
            self._visual_degraded = True
            if not self._arm_last_error:
                self._arm_last_error = "indicator_not_armed"
            first = not self._visual_degrade_warned
            self._visual_degrade_warned = True
            reason = self._arm_last_error
        if first:
            # 只警告一次：叠加层失败会随每次调用重复，逐次打日志等于刷屏。
            # 这一点照抄 control_overlay._warn_display_affinity 的做法。
            logger.warning(
                "AI control indicator is not armed (%s); continuing without it", reason
            )

    def checkpoint(self, token: int) -> None:
        with self._lock:
            request_closed = current_request_closed.get()
            # 「现在真的该停手吗」只看三件事：租约还在、没被抢占、没在主动让位。
            #
            # 这里**不再**要求 `_suppress` 为 True。这是本次修复的核心。
            #
            # `_suppress` 的本意是「物理输入要被拦住，别和 AI 抢」——它保护的是
            # 用户的手，而不是给 AI 发许可证。旧写法把它当成"允许动手"的前置条件，
            # 于是这两件事被绑死了：
            #
            #   1. `__main__` 注册了 `set_health_probe(control_overlay.is_healthy)`，
            #      所以 `_set_locked("ai")` 里 `self._visual_armed = state == "ai" and
            #      self._health_probe is None` 恒为 False ⇒ `_suppress` 也恒为 False；
            #   2. 唯一能把它们翻成 True 的是 `arm_visible()`，而它只有在
            #      `control_overlay.set_active()` 拿到 ack 之后才会被调用；
            #   3. overlay ack 超时 / overlay 不健康 / generation 期间被换掉，都会让
            #      `set_active` 返回 False 或抛错 ⇒ `arm_visible` 不被调用。
            #
            # 结果就是：状态已经进了 ai（租约真的拿到了），但"可以动输入"的开关
            # 没打开，`checkpoint()` 第一条就抛 CONTROL_PREEMPTED，且 executed_steps
            # 恒为 0 —— 一步都没走就被自己拒掉，重试几次里总有一次 overlay 抢上了，
            # 于是表现为"有时能成功"。
            #
            # 现在改为只在**真的在暂停**时拒：`_visual_paused`（截图期间主动把桌面
            # 交出去）或者「已 armed 但压制被撤掉了」（停机、故障恢复的中间态）。
            # 叠加层没画出来不再是拦路理由——它是给人看的提示，失败就降级。
            paused_for_input = self._visual_paused or (
                self._visual_armed and not self._suppress
            )
            if (
                (request_closed is not None and request_closed.is_set())
                or self._stop.is_set()
                or self._state != "ai"
                or self._generation != token
                or self._fast_takeover
                or self._fast_pending
                or self._rotating
                or self._emergency
                or paused_for_input
            ):
                status = self._snapshot_locked(time.monotonic())
                status["executed_steps"] = get_step_count()
                raise ControlBlocked("CONTROL_PREEMPTED", status)

    def checkpoint_current(self) -> None:
        """Validate each desktop input step inside a guarded MCP call."""
        token = current_token.get()
        if token is not None:
            self.checkpoint(token)

    def record_step_current(self) -> None:
        counter = current_steps.get()
        if current_token.get() is not None and counter is not None:
            counter.value += 1

    def physical_key_down(self, vk: int) -> bool:
        # A swallowed physical down did not reach the app, so the AI must
        # release its own injected down when its tool is preempted. Hook
        # callbacks mutate this set concurrently: use atomic membership checks
        # rather than iterating it while resolving left/right modifiers.
        normalized = _key(vk)
        aliases = {
            0x10: (0x10, 0xA0, 0xA1),
            0x11: (0x11, 0xA2, 0xA3),
            0x12: (0x12, 0xA4, 0xA5),
        }
        return any(key in self._delivered_keys for key in aliases.get(normalized, (vk,)))

    def physical_mouse_down(self, button: str) -> bool:
        return {"left": 1, "right": 2, "middle": 3}[button] in self._delivered_mouse

    def end_call(self, token: int) -> None:
        with self._lock:
            self._active_calls = max(0, self._active_calls - 1)
            if self._active_calls == 0:
                self._active_call_since = None
        # 这一轮结束了，登记一个"该撤光了"的时刻。真正的交还在看门狗线程里
        # 按宽限期执行（`_glow_release_due`）——这里不能直接撤：连发调用会把
        # 叠加层闪成一片。签名和计数逻辑保持原样，只在结尾加这一句。
        self.schedule_glow_release()

    def release(self) -> dict:
        """End AI control at the agent's request; a running call stops at its next checkpoint."""
        now = time.monotonic()
        with self._lock:
            changed = self._state == "ai" and self._set_locked("ready")
            result = self._snapshot_locked(now)
        if changed:
            self._notify(result)
        # 交还控制权后清掉截图缓存——**不看 changed**。
        #
        # 控制权在 AI 手上时，渐变光和提示面板会画进截图里；交还后那些装饰消失，
        # 但帧哈希缓存还留着「上一帧」。下次截图如果画面碰巧命中旧帧，就会把
        # 带光晕的旧图当成当前画面返回，看起来像光效卡死了。
        #
        # 为什么不能放在 `if changed:` 里面：那个条件要求 state 恰好是 "ai"。
        # 实测里 release() 被调用时 state 往往是 "unavailable" 或已经是 "ready"，
        # changed 为 False，清理就被跳过了——而用户正是在这时候发现旧图还在。
        # 清缓存很便宜（一次字典 clear），无条件执行没有代价。
        try:
            from windows_mcp.desktop.frame_cache import get_frame_cache
            get_frame_cache().clear()
        except Exception:  # noqa: BLE001 — 清缓存失败不该影响交还控制权
            logger.debug("交还控制权后清截图缓存失败", exc_info=True)
        return result

    def start(self) -> None:
        if not self.enabled:
            self.input_ledger.enable()
            return  # No physical input monitor, watchdog, or ownership notifications.
        if self._thread and self._thread.is_alive():
            if self._stop.is_set():
                raise RuntimeError("Previous physical input monitor has not stopped")
            return
        self._stop.clear()
        self._fault_token = object()
        self._rehook_completed = None
        self._release_requested.clear()
        self._ready.clear()
        self._startup_error = None
        if not _interactive_desktop():
            raise RuntimeError("No active interactive desktop for physical input control")
        self.input_ledger.release_all()
        self.input_ledger.enable()
        self._emergency = False
        self._fail_reason = None
        self._fast_takeover = False
        self._fast_pending = False
        self._rotating = False
        self._thread = threading.Thread(target=self._run, name="windows-mcp-input", daemon=True)
        self._thread.start()
        if not self._ready.wait(3.0) or self._startup_error:
            self.stop()
            raise RuntimeError("Physical input monitor could not start") from self._startup_error
        self._watchdog = threading.Thread(
            target=self._watch, name="windows-mcp-input-watch", daemon=True
        )
        self._watchdog.start()
        with self._lock:
            now = time.monotonic()
            # A new server must not impose a cooldown without physical input.
            elapsed = now - self._last_physical_event
            recent_input = self._last_physical_event > 0 and elapsed < self.idle_timeout_seconds
            if recent_input:
                self._last_user = self._last_physical_event
            self._set_locked("user" if recent_input else "ready")
            result = self._snapshot_locked(now)
        self._notify(result)

    def stop(self) -> None:
        if not self.enabled:
            self.input_ledger.block_new()
            self.input_ledger.release_all()
            return
        self._stop.set()  # Publish shutdown before revoking input permissions.
        self._suppress = False
        self._deadline = 0.0
        self.input_ledger.block_new()
        self._release_requested.set()  # Wake the watchdog before joining it.
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._watchdog:
            self._watchdog.join(timeout=1.0)
        with self._lock:
            changed = self._set_locked("unavailable")
            result = self._snapshot_locked(time.monotonic())
        try:
            self.input_ledger.release_all()
        finally:
            if changed:
                self._notify(result)
        if (self._thread and self._thread.is_alive()) or (
            self._watchdog and self._watchdog.is_alive()
        ):
            raise RuntimeError("Physical input monitor did not stop cleanly")
        self._events = queue.Queue(maxsize=4096)
        self._pressed.clear()
        self._mouse_down.clear()
        self._delivered_keys.clear()
        self._delivered_mouse.clear()
        self._quarantine.clear()
        self._thread = self._watchdog = None

    def fail_open_with_reason(self, reason: str | None) -> None:
        """Fail open and remember *reason* for the unavailable status."""
        self._fail_open(reason=reason)

    def _fail_open(self, reason: str | None = None) -> None:
        if reason:
            self._fail_reason = reason
        self._fault_token = object()  # Invalidate any older successful hook installation.
        self._suppress = False
        self._deadline = 0.0
        self.input_ledger.block_new()  # Atomic flag; hooks must never wait for AI injection.
        self._emergency = True
        self._rotate = True
        # Hook callbacks must not wait on SendInput or the ledger lock. Wake
        # the existing watchdog to clear AI holds immediately off-hook.
        self._release_requested.set()

    def _mark_unavailable(self) -> None:
        # Clear injected holds before any visual or MCP listener can delay us.
        self.input_ledger.block_new()
        try:
            self.input_ledger.release_all()
        except Exception:
            pass  # Keep failed holds for the next watchdog/recovery attempt.
        if not self._lock.acquire(blocking=False):
            return
        try:
            changed = self._set_locked("unavailable")
            result = self._snapshot_locked(time.monotonic())
        finally:
            self._lock.release()
        if changed:
            self._notify(result)

    def _recover_after_rehook(
        self, fault=None, *, healthy: bool | None = None, sampled_at: float | None = None
    ) -> None:
        """Reopen the lease only after hooks, display and held inputs are safe."""
        if healthy is not None and sampled_at is None:
            return  # A caller-supplied health result must include its observation time.
        fault = self._fault_token if fault is None else fault

        def current():
            if fault is not self._fault_token or self._stop.is_set():
                return False
            return sampled_at is None or time.monotonic() - sampled_at < 0.25

        if not self._emergency or not current():
            return
        try:
            self.input_ledger.release_all()
            if self.input_ledger.pending():
                return
            # Production supplies its freshly throttled health result. Direct
            # callers must obtain their own proof, never reuse a cached result.
            if healthy is None:
                healthy = (
                    self._health_probe is None or self._health_probe()
                ) and _interactive_desktop()
                sampled_at = time.monotonic()
            if not healthy:
                return
            with self._lock:
                if not current():
                    return
                self.input_ledger.enable()
                if not current():
                    self.input_ledger.block_new()
                    return
                self._emergency = False
                self._fail_reason = None
                self._fast_takeover = False
                self._fast_pending = False
                self._last_user = time.monotonic()
                changed = self._set_locked("user")
                if not current():
                    self._emergency = True
                    self._set_locked("unavailable")
                    self.input_ledger.block_new()
                    return
                result = self._snapshot_locked(self._last_user)
        except Exception:
            return  # A failed release or health check keeps control unavailable.
        if changed:
            self._notify(result)

    def _queue(self, event: tuple) -> None:
        try:
            self._events.put_nowait(event)
            self._release_requested.set()  # Wake coordination without blocking the hook pump.
        except queue.Full:
            self._fail_open()

    def _physical_mouse(self, code, wparam, lparam):
        return control_hooks.physical_mouse(self, code, wparam, lparam)

    def _physical_key(self, code, wparam, lparam):
        return control_hooks.physical_key(self, code, wparam, lparam)

    def _raw_input(self, lparam) -> None:
        control_hooks.raw_input(self, lparam)

    def _handle(self, event: tuple) -> None:
        now = time.monotonic()
        with self._lock:
            if self._emergency:
                changed = self._set_locked("unavailable")
            else:
                changed = self._tick_locked(now)
                kind = event[0]
                # Queued events carry no stamp; if the newest input predates the
                # consent floor, so does every event still waiting in the queue.
                user_input = (
                    kind in ("key", "point", "raw")
                    and self._last_physical_event >= self._input_floor
                )
                if user_input and self._state == "ai" and not self._visual_armed:
                    # Physical input was delivered while the indicator was not armed.
                    self._last_user = now
                    changed = self._set_locked("user") or changed
                # The tick above may expire the AI lease before this queued
                # takeover is consumed. Always acknowledge its fast-path flag.
                if kind == "hotkey":
                    self._last_user = now
                    changed = self._set_locked("user") or changed
                    self._fast_takeover = False
                elif user_input and self._state in ("ready", "user"):
                    self._last_user = now
                    changed = self._set_locked("user") or changed
                elif kind == "raw" and self._state == "ai":
                    device, dx, dy = event[1:4]
                    stamp = event[4] if len(event) > 4 else now
                    if (dx or dy) and stamp >= max(self._motion_since, self._input_floor):
                        if device != self._raw_device or stamp - self._last_move >= 0.5:
                            self._raw_distance = 0.0
                        self._raw_device = device
                        self._last_move = stamp
                        # Sum path length, not signed displacement. Keep motion
                        # in native device units; hook coordinates include AI
                        # cursor movement and must not contribute a second time.
                        self._raw_distance += math.hypot(dx, dy)
                        if self._raw_distance >= self.mouse_takeover_units:
                            self._last_user = now
                            changed = self._set_locked("user") or changed
            result = self._snapshot_locked(now)
        if changed:
            self._notify(result)

    def _install_hooks(self, instance) -> None:
        mouse = _user32.SetWindowsHookExW(14, self._mouse_callback, instance, 0)
        key = _user32.SetWindowsHookExW(13, self._key_callback, instance, 0)
        if not mouse or not key:
            if mouse:
                _user32.UnhookWindowsHookEx(mouse)
            if key:
                _user32.UnhookWindowsHookEx(key)
            raise ctypes.WinError(ctypes.get_last_error())
        old_mouse, old_key = self._mouse_hook, self._key_hook
        self._mouse_hook, self._key_hook = mouse, key
        for old in (old_mouse, old_key):
            if old:
                _user32.UnhookWindowsHookEx(old)

    def _run(self) -> None:
        # Keep the Win32 message pump alongside its raw-input structures.
        run_input_monitor(self)

    def _watch(self) -> None:
        run_control_worker(self, _interactive_desktop)


_controller = ControlCoordinator()


def get_controller() -> ControlCoordinator:
    return _controller

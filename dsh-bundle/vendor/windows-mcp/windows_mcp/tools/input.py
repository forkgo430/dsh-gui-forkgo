"""Input tools — Click, Type, Scroll, Move, Shortcut, Wait, WaitFor."""

import json
import math
import re
import time
from collections.abc import Callable, Iterator
from typing import Any, Literal

from mcp.types import ToolAnnotations
from windows_mcp.desktop.utils import as_bool as _as_bool
from windows_mcp.infrastructure import with_analytics
from fastmcp import Context


WaitForCondition = Literal[
    "text_exists",
    "active_window",
    "element_exists",
    "element_enabled",
    "focused_element",
]


def _resolve_label(desktop: Any, label: int) -> list[int]:
    """Resolve a UI element label to screen coordinates."""
    if desktop.desktop_state is None:
        raise ValueError('Desktop state is empty. Please run Screen with mode="full" first.')
    try:
        return list(desktop.get_coordinates_from_label(label))
    except Exception as e:
        raise ValueError(f"Failed to find element with label {label}: {e}")


def _validate_finite_number(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")


def _as_loc(value: list | str | None) -> list | None:
    """Coerce a JSON-stringified list back to a list.

    Claude Desktop strips anyOf schemas and the model serializes lists as
    strings (e.g. '[100, 200]'). Parsing here keeps the tools working.
    """
    if value is None or isinstance(value, list):
        return value
    return json.loads(value)


# One optional leading sign followed by decimal digits — exactly what int()
# accepts. `.lstrip("+-").isdigit()` used to strip *any* run of signs, so
# "+-5"/"--5" passed the check and then int() raised a confusing low-level
# error instead of the intended message; `.isdigit()` also accepts digits
# int() rejects (e.g. the superscript "²").
_SIGNED_INT_RE = re.compile(r"^[+-]?\d+$")


def _as_point(value: object, name: str) -> list[int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{name} must be a list of exactly 2 integers [x, y]")
    parsed = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError(f"{name} must contain integers, not booleans")
        if isinstance(item, int):
            parsed.append(item)
            continue
        if isinstance(item, str):
            stripped = item.strip()
            if _SIGNED_INT_RE.match(stripped):
                parsed.append(int(stripped))
                continue
        raise ValueError(f"{name} must contain exactly 2 integers")
    return parsed


def _text_matches(value: object | None, expected: str | None) -> bool:
    if expected is None:
        return True
    if value is None:
        return False
    return expected.casefold() in str(value).casefold()


def _metadata_text_matches(metadata: dict[str, object], expected: str | None) -> bool:
    return any(_text_matches(value, expected) for value in metadata.values())


def _iter_nodes(desktop_state: Any) -> Iterator[Any]:
    tree_state = getattr(desktop_state, "tree_state", None)
    if tree_state is None:
        return
    yield from getattr(tree_state, "interactive_nodes", [])
    yield from getattr(tree_state, "scrollable_nodes", [])


def _iter_text_sources(desktop_state: Any) -> Iterator[object]:
    active_window = getattr(desktop_state, "active_window", None)
    if active_window is not None:
        yield active_window.name

    for window in getattr(desktop_state, "windows", []):
        yield window.name

    tree_state = getattr(desktop_state, "tree_state", None)
    if tree_state is None:
        return

    for node in _iter_nodes(desktop_state):
        yield node.name
        yield node.control_type
        yield node.window_name
        for value in getattr(node, "metadata", {}).values():
            yield value

    for node in getattr(tree_state, "dom_informative_nodes", []):
        yield getattr(node, "text", "")


def _node_matches(node: Any, text: str | None, window_name: str | None) -> bool:
    metadata: dict[str, object] = getattr(node, "metadata", {})
    return (
        _text_matches(getattr(node, "name", ""), text)
        or _text_matches(getattr(node, "control_type", ""), text)
        or _metadata_text_matches(metadata, text)
    ) and _text_matches(getattr(node, "window_name", ""), window_name)


def _matches_wait_condition(
    desktop_state: Any,
    condition: WaitForCondition,
    text: str | None,
    window_name: str | None,
) -> tuple[bool, str]:
    if condition == "text_exists":
        for source in _iter_text_sources(desktop_state):
            if _text_matches(source, text):
                return True, f"text {text!r} appeared"
        return False, f"text {text!r} was absent"

    if condition == "active_window":
        expected = window_name or text
        active_window = getattr(desktop_state, "active_window", None)
        active_name = active_window.name if active_window else ""
        if _text_matches(active_name, expected):
            return True, f"active window matched {active_name!r}"
        return False, f"active window was {active_name!r}"

    if condition in {"element_exists", "element_enabled"}:
        for node in _iter_nodes(desktop_state):
            if _node_matches(node, text, window_name):
                return True, f"element matched {getattr(node, 'name', '')!r}"
        return False, "matching element was absent"

    if condition == "focused_element":
        for node in _iter_nodes(desktop_state):
            metadata = getattr(node, "metadata", {})
            if metadata.get("has_focused") and _node_matches(node, text, window_name):
                return True, f"focused element matched {getattr(node, 'name', '')!r}"
        return False, "matching focused element was absent"

    raise ValueError(f"Unsupported WaitFor condition: {condition}")


def _validate_wait_for_args(
    condition: str,
    text: str | None,
    window_name: str | None,
    timeout: float,
    interval: float,
) -> WaitForCondition:
    _validate_finite_number(timeout, "timeout")
    _validate_finite_number(interval, "interval")

    normalized = condition.strip().lower().replace("-", "_")
    aliases = {
        "text": "text_exists",
        "window": "active_window",
        "element": "element_exists",
        "enabled": "element_enabled",
        "focused": "focused_element",
    }
    normalized = aliases.get(normalized, normalized)
    valid_conditions = {
        "text_exists",
        "active_window",
        "element_exists",
        "element_enabled",
        "focused_element",
    }
    if normalized not in valid_conditions:
        raise ValueError(
            "condition must be one of: text_exists, active_window, element_exists, "
            "element_enabled, focused_element"
        )

    if timeout <= 0 or timeout > 120:
        raise ValueError("timeout must be greater than 0 and at most 120 seconds")
    if interval <= 0 or interval > 5:
        raise ValueError("interval must be greater than 0 and at most 5 seconds")

    if normalized == "text_exists" and not text:
        raise ValueError("text is required when condition is text_exists")
    if normalized == "active_window" and not (text or window_name):
        raise ValueError("text or window_name is required when condition is active_window")
    if normalized in {"element_exists", "element_enabled"} and not (text or window_name):
        raise ValueError(
            "text or window_name is required when condition is element_exists or element_enabled"
        )

    return normalized


def _clipboard_get() -> str:
    """读系统剪贴板文本。原 Clipboard 工具 mode="get" 的实现，原样搬过来。"""
    import win32clipboard

    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
            data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            return f"Clipboard content:\n{data}"
        return "Clipboard is empty or contains non-text data."
    finally:
        win32clipboard.CloseClipboard()


# ---------------------------------------------------------------- 动作序列
# 对应 pilot 的 desktop_sequence（lib/index.js:1321-1468 / 1848-1935）。
#
# 为什么要这个东西：一次 Input 只能做一个动作，那"看→做→看"这个环就必须靠
# 多次工具往返闭合，每一次往返都要重新读一遍上下文、重新推一遍现在在哪。允许
# 一次调用跑多步，环就闭合得快；但步与步之间必须留够时间，否则会点到还没画出来
# 的东西上 —— 这就是 _SEQUENCE_STEP_SETTLE_S 存在的唯一理由。
SEQUENCE_ACTIONS = (
    "click",
    "type",
    "scroll",
    "move",
    "shortcut",
    "wait",
    "wait_for",
    "multi_select",
    "multi_edit",
    "clipboard_get",
    "clipboard_set",
)

# pilot 的 stepSettleMs 默认 120ms（lib/index.js:40）。实测这个量级够一个普通
# 控件把状态画出来，又不至于让 24 步的批次拖出三秒空等。
_SEQUENCE_STEP_SETTLE_S = 0.12
# pilot 的 maxSequenceSteps 默认 24（lib/index.js:42）。上限的意义不是省事：
# 步数越多，越后面那几步用的越可能是批次开始前的旧画面，错得越离谱。
MAX_SEQUENCE_STEPS = 24


def _normalize_sequence_steps(steps: object, interval: float) -> list[dict]:
    """在任何动作落到桌面之前，把整批步骤校验完。

    为什么先校验：一个格式错的批次应该在**一次往返**里被拒掉，而不是走到第 5 步
    才发现第 6 步缺参数 —— 那时候前 5 步已经真的点下去了，界面已经变了，而调用方
    手里没有任何办法回退。（pilot 的注释说的是同一件事：a malformed batch costs
    one round trip instead of half a gesture。）
    """
    if steps is None:
        raise ValueError('sequence 需要 steps，形如 [{"action":"click","loc":[100,200]}, ...]')
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except ValueError:
            raise ValueError("steps 必须是 JSON 数组")
    if not isinstance(steps, list):
        raise ValueError("steps 必须是数组")
    if not steps:
        raise ValueError("sequence 至少需要一步")
    if len(steps) > MAX_SEQUENCE_STEPS:
        raise ValueError(
            f"sequence 一次最多 {MAX_SEQUENCE_STEPS} 步（收到 {len(steps)} 步）；"
            f"拆成几批，或者改用多次单独的 Input 调用"
        )

    normalized: list[dict] = []
    for offset, step in enumerate(steps):
        position = offset + 1
        if isinstance(step, str):
            try:
                step = json.loads(step)
            except ValueError:
                raise ValueError(f"第 {position} 步不是合法的 JSON 对象")
        if not isinstance(step, dict):
            raise ValueError(f"第 {position} 步必须是对象，形如 {{\"action\": \"click\", ...}}")

        action = step.get("action")
        if not isinstance(action, str) or not action.strip():
            raise ValueError(f"第 {position} 步缺少 action")
        action = action.strip().lower()
        if action == "sequence":
            # 明确拦住：嵌套序列会让"第几步失败"的记账失去意义，也让步数上限失效。
            raise ValueError(
                f"第 {position} 步是 sequence —— 序列里不能再套序列；"
                f"把内层步骤平铺到 steps 里"
            )
        if action not in SEQUENCE_ACTIONS:
            raise ValueError(
                f"第 {position} 步的 action {action!r} 不认识；可用："
                + " / ".join(SEQUENCE_ACTIONS)
            )

        item = dict(step)
        item["action"] = action
        # 每步可以带自己的 interval（等价于 pilot 的 per-step stepSettleMs），
        # 没带就用批次级的。负数、非数字一律拒掉：那只会让界面来不及反应，
        # 也就是点到还没画出来的东西上。
        step_interval = item.get("interval", interval)
        try:
            step_interval = float(step_interval)
        except (TypeError, ValueError):
            raise ValueError(f"第 {position} 步的 interval 必须是数字")
        if not math.isfinite(step_interval) or step_interval < 0:
            raise ValueError(f"第 {position} 步的 interval 必须是不小于 0 的有限数")
        if step_interval > 5:
            raise ValueError(f"第 {position} 步的 interval 最多 5 秒（收到 {step_interval}）")
        item["interval"] = step_interval
        normalized.append(item)
    return normalized


def _run_sequence(
    steps: list[dict],
    dispatch: Callable[[dict], str],
) -> str:
    """按顺序跑一批步骤，逐步记账；某步失败就停下并说清停在哪。

    为什么不"出错继续"：批次里的步骤通常是有依赖的（填完用户名才能填密码、点开
    菜单才能点菜单项）。第 3 步失败还硬跑第 4、5 步，等于在一个已经不是预期的界面
    上乱点 —— 比直接停下危险得多。停下时把"第几步失败、前面哪些已经做完"写清楚，
    调用方才有办法接着往下走。
    """
    total = len(steps)
    executed = 0
    lines: list[str] = [f"Sequence: {total} 步"]

    for index, step in enumerate(steps, start=1):
        action = step["action"]
        started = time.monotonic()
        try:
            note = dispatch(step)
        except Exception as exc:  # noqa: BLE001 — 逐步记账，异常转成结构化的失败项
            elapsed_ms = int((time.monotonic() - started) * 1000)
            reason = f"{type(exc).__name__}: {exc}"
            lines.append(
                f"  [{index}/{total}] {action} FAILED ({elapsed_ms}ms) — {reason}"
            )
            done = f"已完成 {executed} 步（第 1..{executed} 步）" if executed else "没有任何一步被执行"
            lines.append(
                f"Sequence Stopped: 第 {index} 步失败，批次在此停下；{done}。"
                f"后面 {total - index} 步没有被执行 —— 界面现在停在失败那一步之后的状态，"
                f"先 Screen 看一眼再决定怎么继续。"
            )
            return "\n".join(lines)
        executed += 1
        elapsed_ms = int((time.monotonic() - started) * 1000)
        detail = f" — {note}" if note else ""
        lines.append(f"  [{index}/{total}] {action} ok ({elapsed_ms}ms){detail}")

        # 步间停顿：最后一步之后不用等，那只会白等一次。
        if index < total and step["interval"] > 0:
            time.sleep(step["interval"])

    lines.append(f"Sequence Done: {executed}/{total} 步全部执行。")
    return "\n".join(lines)


def _clipboard_set(text: str) -> str:
    """写系统剪贴板文本。原 Clipboard 工具 mode="set" 的实现，原样搬过来。"""
    if text is None:
        return "Error: text parameter required for clipboard_set."
    import win32clipboard

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        return f"Clipboard set to: {text[:100]}{'...' if len(text) > 100 else ''}"
    finally:
        win32clipboard.CloseClipboard()


def register(
    mcp: Any,
    *,
    get_desktop: Callable[[], Any],
    get_analytics: Callable[[], Any],
) -> None:
    def click_tool(
        loc: list[int] | str | None = None,
        label: int | None = None,
        locs: list[list] | str | None = None,
        labels: list | str | None = None,
        press_ctrl: bool | str = True,
        button: Literal["left", "right", "middle"] = "left",
        clicks: int = 1,
        ctx: Context = None,
    ) -> str:
        desktop = get_desktop()
        loc = _as_loc(loc)
        if loc is None and label is None:
            raise ValueError("Either loc or label must be provided.")
        # For a negative count Desktop.click() made one left click, or no right or
        # middle click, and the reply began with "None".
        if clicks < 0:
            raise ValueError("clicks must be 0 or more")
        if label is not None:
            loc = _resolve_label(desktop, label)
        if len(loc) != 2:
            raise ValueError("Location must be a list of exactly 2 integers [x, y]")
        x, y = loc[0], loc[1]
        warning = desktop.click(loc=loc, button=button, clicks=clicks)
        num_clicks = {0: "Hover", 1: "Single", 2: "Double", 3: "Triple"}
        answer = f"{num_clicks.get(clicks, f'{clicks}x')} {button} clicked at ({x},{y})."
        # A click at a disabled window is dropped by Windows, so say what blocked it.
        return f"{answer} {warning}" if warning else answer

    def type_tool(
        text: str,
        loc: list[int] | str | None = None,
        label: int | None = None,
        clear: bool | str = False,
        caret_position: Literal["start", "idle", "end"] = "idle",
        press_enter: bool | str = False,
        ctx: Context = None,
    ) -> str:
        desktop = get_desktop()
        loc = _as_loc(loc)
        clear = _as_bool(clear, "clear")
        press_enter = _as_bool(press_enter, "press_enter")
        if loc is None and label is None:
            raise ValueError("Either loc or label must be provided.")
        if label is not None:
            loc = _resolve_label(desktop, label)
        if len(loc) != 2:
            raise ValueError("Location must be a list of exactly 2 integers [x, y]")
        x, y = loc[0], loc[1]
        note = desktop.type(
            loc=loc,
            text=text,
            caret_position=caret_position,
            clear=clear,
            press_enter=press_enter,
        )
        result = f"Typed {text} at ({x},{y})."
        return f"{result} {note}" if isinstance(note, str) and note else result

    def scroll_tool(
        loc: list[int] | str | None = None,
        label: int | None = None,
        type: Literal["horizontal", "vertical"] = "vertical",
        direction: Literal["up", "down", "left", "right"] = "down",
        wheel_times: int = 1,
        ctx: Context = None,
    ) -> str:
        desktop = get_desktop()
        loc = _as_loc(loc)
        if label is not None:
            loc = _resolve_label(desktop, label)
        if loc and len(loc) != 2:
            raise ValueError("Location must be a list of exactly 2 integers [x, y]")
        desktop.scroll(loc, type, direction, wheel_times)
        # The conditional used to cover the whole message, so a scroll at the
        # mouse position (loc=None) answered with an empty string.
        where = f"({loc[0]},{loc[1]})" if loc else "the current mouse position"
        return f"Scrolled {type} {direction} by {wheel_times} wheel times at {where}."

    def move_tool(
        loc: list[int] | str | None = None,
        label: int | None = None,
        drag: bool | str = False,
        from_loc: list[int] | str | None = None,
        duration: float | int | str | None = None,
        ctx: Context = None,
    ) -> str:
        desktop = get_desktop()
        loc = _as_loc(loc)
        from_loc = _as_loc(from_loc)
        drag = _as_bool(drag, "drag")
        if loc is None and label is None:
            raise ValueError("Either loc or label must be provided.")
        if label is not None:
            loc = _resolve_label(desktop, label)
        if not isinstance(loc, list) or len(loc) != 2:
            raise ValueError("loc must be a list of exactly 2 integers [x, y]")
        if from_loc is not None and (not isinstance(from_loc, list) or len(from_loc) != 2):
            raise ValueError("from_loc must be a list of exactly 2 integers [x, y]")
        has_drag_only_options = any(
            value is not None
            for value in (
                from_loc,
                duration,
            )
        )
        if has_drag_only_options and not drag:
            raise ValueError("from_loc and duration require drag=True")
        if drag:
            loc = _as_point(loc, "loc")
            if from_loc is not None:
                from_loc = _as_point(from_loc, "from_loc")
        x, y = loc[0], loc[1]
        if drag:
            result = desktop.drag(
                loc,
                from_loc=from_loc,
                duration=duration,
            )
            start_x, start_y = result["start"]
            effective_duration = result["duration"]
            if effective_duration is None:
                return f"Dragged from ({start_x},{start_y}) to ({x},{y})."
            return (
                f"Dragged from ({start_x},{start_y}) to ({x},{y}) "
                f"over {effective_duration:.3f} seconds."
            )
        else:
            desktop.move(loc)
            return f"Moved the mouse pointer to ({x},{y})."

    def shortcut_tool(shortcut: str, ctx: Context = None):
        get_desktop().shortcut(shortcut)
        return f"Pressed {shortcut}."

    def wait_tool(duration: int, ctx: Context = None) -> str:
        time.sleep(duration)
        return f"Waited for {duration} seconds."

    def wait_for_tool(
        condition: str,
        text: str | None = None,
        window_name: str | None = None,
        timeout: float = 10.0,
        interval: float = 0.25,
        use_dom: bool | str = False,
        ctx: Context = None,
    ) -> str:
        normalized = _validate_wait_for_args(
            condition=condition,
            text=text,
            window_name=window_name,
            timeout=timeout,
            interval=interval,
        )
        desktop = get_desktop()
        use_dom_bool = _as_bool(use_dom, "use_dom")
        started_at = time.monotonic()
        deadline = started_at + timeout
        attempts = 0
        last_detail = "condition was not evaluated"

        while True:
            attempts += 1
            desktop_state = desktop.get_state(
                use_vision=False,
                use_dom=use_dom_bool,
                use_ui_tree=True,
                use_annotation=False,
            )
            matched, last_detail = _matches_wait_condition(
                desktop_state=desktop_state,
                condition=normalized,
                text=text,
                window_name=window_name,
            )
            if matched:
                elapsed = time.monotonic() - started_at
                return (
                    f"WaitFor condition '{normalized}' satisfied after "
                    f"{elapsed:.2f}s and {attempts} attempt(s): {last_detail}."
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out after {timeout:.2f}s waiting for '{normalized}': {last_detail}."
                )
            time.sleep(min(interval, remaining))



    def multi_select_tool(
        locs: list[list[int]] | str | None = None,
        labels: list[int] | str | None = None,
        press_ctrl: bool | str = True,
        ctx: Context = None,
    ) -> str:
        desktop = get_desktop()
        locs = _as_loc(locs)
        labels = _as_loc(labels)
        if locs is None and labels is None:
            raise ValueError("Either locs or labels must be provided.")
        locs = locs or []
        if labels is not None:
            if desktop.desktop_state is None:
                raise ValueError('Desktop state is empty. Please run Screen with mode="full" first.')
            try:
                resolved_locs = desktop.get_coordinates_from_labels(labels)
                locs.extend([list(loc) for loc in resolved_locs])
            except Exception as e:
                raise ValueError(f"Failed to resolve labels {labels}: {e}")

        press_ctrl = _as_bool(press_ctrl, "press_ctrl")
        desktop.multi_select(press_ctrl, locs)
        elements_str = "\n".join([f"({loc[0]},{loc[1]})" for loc in locs])
        return f"Multi-selected elements at:\n{elements_str}"

    def multi_edit_tool(
        locs: list[list] | str | None = None,
        labels: list[list] | str | None = None,
        ctx: Context = None,
    ) -> str:
        desktop = get_desktop()
        locs = _as_loc(locs)
        labels = _as_loc(labels)
        if locs is None and labels is None:
            raise ValueError("Either locs or labels must be provided.")
        locs = locs or []
        if labels is not None:
            if desktop.desktop_state is None:
                raise ValueError('Desktop state is empty. Please run Screen with mode="full" first.')

            # Pre-validate and extract labels and texts
            processed_labels = []
            for item in labels:
                if len(item) != 2:
                    raise ValueError(f"Each label item must be [label, text]. Invalid: {item}")
                try:
                    processed_labels.append((int(item[0]), item[1]))
                except (ValueError, TypeError):
                    raise ValueError(f"Invalid label id in item: {item}")

            try:
                label_ids = [item[0] for item in processed_labels]
                resolved_coords = desktop.get_coordinates_from_labels(label_ids)
                for (x, y), (_, text) in zip(resolved_coords, processed_labels):
                    locs.append([x, y, text])
            except Exception as e:
                raise ValueError(f"Failed to process labels: {e}")

        notes = desktop.multi_edit(locs)
        elements_str = ", ".join([f"({e[0]},{e[1]}) with text '{e[2]}'" for e in locs])
        warnings = [n for n in notes or [] if isinstance(n, str) and n.startswith("Warning")]
        result = f"Multi-edited elements at: {elements_str}"
        return "\n".join([result, *warnings])

    @mcp.tool(
        name="Input",
        description=(
            "鼠标、键盘、剪贴板、等待，一个入口。按 action 分发："
            "click=按坐标或控件点击；type=输入文字；scroll=滚轮；move=移动指针或拖拽；"
            "shortcut=快捷键组合；wait=固定等待；wait_for=等条件成立；"
            "multi_select=批量选或多点；multi_edit=批量填多格；"
            "clipboard_get=读系统剪贴板；clipboard_set=写系统剪贴板（text 传内容）；"
            "sequence=一次调用跑多步（steps 是步骤数组，每步形如 "
            "{\"action\":\"click\",\"loc\":[100,200]}，复用上面所有 action）。"
            "click 支持 button（left/right/middle）与 clicks（0=悬停 1=单击 2=双击），"
            "按控件比按坐标稳。type 的 clear=True 清空再输。"
            "wait_for 的 condition 可选 text_exists / active_window / element_exists / "
            "element_enabled / focused_element。"
            "sequence 默认步间停顿 120ms（interval 覆盖，单步也可带自己的 interval），"
            "一次最多 24 步，序列里不能再套序列；某步失败就停下并说明停在第几步、"
            "前面哪些已经做完 —— 因为批次里的步骤通常有依赖，接着跑等于在错误的界面上乱点。"
            "clipboard 是真实的系统剪贴板，set 会覆盖用户已复制的内容；别拿它当工具之间的传参通道。"
        ),
        annotations=ToolAnnotations(
            title="Input",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Input-Tool", run_sync_in_thread=False)
    def input_tool(
        action: str,
        loc: list[int] | str | None = None,
        label: int | None = None,
        button: Literal["left", "right", "middle"] = "left",
        clicks: int = 1,
        text: str = "",
        clear: bool | str = False,
        caret_position: Literal["start", "idle", "end"] = "idle",
        press_enter: bool | str = False,
        direction: str = "down",
        wheel_times: int = 1,
        amount: int = 3,
        to_x: int = 0,
        to_y: int = 0,
        drag: bool | str = False,
        duration: float | int | str | None = None,
        shortcut: str = "",
        condition: str = "",
        window_name: str | None = None,
        timeout: float = 10.0,
        interval: float | None = None,
        use_dom: bool | str = False,
        type: Literal["horizontal", "vertical"] = "vertical",
        locs: list[list] | str | None = None,
        labels: list | str | None = None,
        press_ctrl: bool | str = True,
        steps: list[dict] | str | None = None,
        ctx: Context = None,
    ) -> str:
        # 所有 action 的默认值集中在一处：sequence 的每一步都要拿它当底，
        # 单步里没写的字段（比如 button）才有确定的取值，而不是各走各的分支。
        params: dict[str, Any] = {
            "action": action,
            "loc": loc,
            "label": label,
            "button": button,
            "clicks": clicks,
            "text": text,
            "clear": clear,
            "caret_position": caret_position,
            "press_enter": press_enter,
            "direction": direction,
            "wheel_times": wheel_times,
            "amount": amount,
            "to_x": to_x,
            "to_y": to_y,
            "drag": drag,
            "duration": duration,
            "shortcut": shortcut,
            "condition": condition,
            "window_name": window_name,
            "timeout": timeout,
            # interval 是"等多久"的通用参数，两个消费者的默认值不一样：
            # wait_for 轮询要 0.25s（上游行为），sequence 步间停顿要 0.12s。
            # 所以声明成 None 表示"没传"，由各自的消费者补自己的默认值 ——
            # 写死成 0.25 会让 sequence 的每一步都白等一倍，写死成 0.12 会
            # 改掉 wait_for 一直在用的轮询节奏。
            "interval": interval,
            "use_dom": use_dom,
            # 滚轮方向轴叫 type（上游一直这么叫，改名会破坏现有调用方）。
            # 参数名遮掉内建 type 是这个文件原本就有的写法，不是这次引入的；
            # 提示一下免得后来人再踩：函数体里不要再调用内建 type()。
            "type": type,
            "locs": locs,
            "labels": labels,
            "press_ctrl": press_ctrl,
        }
        # sequence 必须在动作表**之外**接：_dispatch_action 是动作表，sequence 是
        # 动作表的执行器，放进动作表自己就会递归。也正因为不在一起，嵌套才拦得住
        # （_normalize_sequence_steps 里对 action=="sequence" 直接报错）。
        if (action or "").strip().lower() == "sequence":
            return _sequence_tool(steps, interval, ctx)
        return _dispatch_action(params, ctx)

    def _dispatch_action(params: dict, ctx: Context = None) -> str:
        """一个动作的分发中心。input_tool 和 sequence 都走这里。

        为什么抽出来：sequence 的每一步必须和单独的 Input 调用**行为完全一致**，
        包括参数解析、报错文案、desktop 方法的调用顺序。另写一份 if/elif 链就等于
        复制一遍逻辑，两份迟早漂移 —— 一边修了另一边没修，batch 与单步就会做出
        不一样的事。
        """
        act = (params.get("action") or "").strip().lower()
        loc = params.get("loc")
        label = params.get("label")
        button = params.get("button") or "left"
        clicks = params.get("clicks", 1)
        text = params.get("text", "")
        clear = params.get("clear", False)
        caret_position = params.get("caret_position", "idle")
        press_enter = params.get("press_enter", False)
        direction = params.get("direction", "down")
        wheel_times = params.get("wheel_times", 1)
        drag = params.get("drag", False)
        duration = params.get("duration")
        shortcut = params.get("shortcut", "")
        condition = params.get("condition", "")
        window_name = params.get("window_name")
        timeout = params.get("timeout", 10.0)
        poll_interval = params.get("interval")
        if poll_interval is None:
            poll_interval = 0.25
        use_dom = params.get("use_dom", False)
        scroll_kind = params.get("type") or "vertical"
        locs = params.get("locs")
        labels = params.get("labels")
        press_ctrl = params.get("press_ctrl", True)

        if act == "click":
            if loc is None and label is None:
                raise ValueError("click 需要 loc 或 label")
            return click_tool(loc=loc, label=label, button=button,
                              clicks=clicks, ctx=ctx)

        if act == "type":
            return type_tool(text=text, loc=loc, label=label, clear=clear,
                             caret_position=caret_position,
                             press_enter=press_enter, ctx=ctx)

        if act == "scroll":
            return scroll_tool(loc=loc, label=label, direction=direction,
                               wheel_times=wheel_times, type=scroll_kind, ctx=ctx)

        if act == "move":
            return move_tool(loc=loc, label=label, drag=drag,
                             from_loc=params.get("from_loc"), duration=duration, ctx=ctx)

        if act == "shortcut":
            if not shortcut:
                raise ValueError("shortcut 需要组合键，例如 'ctrl+c'")
            return shortcut_tool(shortcut=shortcut, ctx=ctx)

        if act == "wait":
            return wait_tool(duration=int(duration or 1), ctx=ctx)

        if act == "wait_for":
            return wait_for_tool(condition=condition, text=text,
                                 window_name=window_name, timeout=timeout,
                                 interval=poll_interval, use_dom=use_dom, ctx=ctx)

        if act == "multi_select":
            return multi_select_tool(locs=locs, labels=labels,
                                     press_ctrl=press_ctrl, ctx=ctx)

        if act == "multi_edit":
            return multi_edit_tool(locs=locs, labels=labels, ctx=ctx)

        # 剪贴板两个动作。原来是独立的 Clipboard 工具（mode=get/set），
        # 合并进来一个新参数都不用加 —— text 本来就在。Input 内部的 _paste_text
        # 本来就在操作剪贴板，语义上是一体的。
        if act == "clipboard_get":
            return _clipboard_get()

        if act == "clipboard_set":
            # 单步写法里写 value/value_text 也能落进剪贴板：那是"内容"的自然名字，
            # 而 text 在批次里专指 type 要打的字，混用会让步骤读起来有歧义。
            body = params.get("body")
            return _clipboard_set(text if body is None else body)

        raise ValueError(
            f"未知 action {params.get('action')!r}。可用：click / type / scroll / move / "
            f"shortcut / wait / wait_for / multi_select / multi_edit / "
            f"clipboard_get / clipboard_set / sequence")

    def _sequence_tool(steps: object, interval: float | None, ctx: Context = None) -> str:
        """action="sequence"：一次调用跑多步。"""
        batch_interval = _SEQUENCE_STEP_SETTLE_S if interval is None else float(interval)
        if not math.isfinite(batch_interval) or batch_interval < 0:
            raise ValueError("interval 必须是不小于 0 的有限数")
        if batch_interval > 5:
            raise ValueError("sequence 的 interval 最多 5 秒")

        normalized = _normalize_sequence_steps(steps, batch_interval)

        # 每步的底 = 声明默认值 + 该步自己写的字段。刻意**不**继承批次调用上的
        # loc / text / label：那样会把"这一批的公共参数"和"某一步的参数"混成一件
        # 事，写错一次就会在 24 步里同一行文字被反复输入。
        step_defaults: dict[str, Any] = {
            "button": "left",
            "clicks": 1,
            "text": "",
            "clear": False,
            "caret_position": "idle",
            "press_enter": False,
            "direction": "down",
            "wheel_times": 1,
            "drag": False,
            "duration": None,
            "shortcut": "",
            "condition": "",
            "window_name": None,
            "timeout": 10.0,
            "use_dom": False,
            "press_ctrl": True,
            "loc": None,
            "label": None,
            "locs": None,
            "labels": None,
            "from_loc": None,
            "type": "vertical",
        }

        def dispatch(step: dict) -> str:
            params = dict(step_defaults)
            # wait 的两种写法：duration（秒，与单步 Input 一致）和 ms（毫秒，
            # pilot 的 sequence 用这个）。都接受，ms 优先。
            if step["action"] == "wait" and step.get("ms") is not None:
                params["duration"] = float(step["ms"]) / 1000.0
            for key, value in step.items():
                if key in ("interval", "ms"):
                    # interval 是**步间**停顿，不是动作参数，不能漏进 desktop 调用；
                    # ms 已经在上面换算成 duration 了。
                    continue
                params[key] = value
            # 剪贴板写入的内容：单步里写 value 或 value_text 都接受。
            # 为什么不一并塞进 text —— text 是 type 的输入，两者语义不同，
            # 混用会让"我这步到底是在打字还是在写剪贴板"变得看不出来。
            body = step.get("value", step.get("value_text"))
            if body is not None:
                params["body"] = str(body)
            return _dispatch_action(params, ctx)

        return _run_sequence(normalized, dispatch)


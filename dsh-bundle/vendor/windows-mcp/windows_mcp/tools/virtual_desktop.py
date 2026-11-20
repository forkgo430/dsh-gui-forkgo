"""VirtualDesktop tool — Windows 10/11 virtual desktops (window groups).

Scope, stated up front because the name is overloaded: this moves *windows*
between Windows virtual desktops. It does not create an isolated login session,
so input focus and the cursor stay shared with whoever is at the keyboard.

The COM plumbing lives in :mod:`windows_mcp.vdm.core`. That module is a thin
wrapper whose mutators log failures and return ``None``, which a tool cannot
report honestly, so every mutation here resolves its target, acts on the
internal manager directly, then re-reads state to prove what happened.

Two structural choices:

* All COM work runs on one dedicated STA thread. ``vdm.core`` keeps a
  thread-local manager, and VirtualDesktopManagerInternal belongs to
  explorer.exe's apartment, so mixing callers across threads produces objects
  that outlive their apartment and fail on the next call. The MCP server is
  async and tool bodies may be dispatched on any pool thread; owning the
  thread here removes that variable. It is deliberately *not* the UIA worker:
  a long ``WaitFor`` must not queue a desktop switch behind it.
* Every read returns ``desktop_id`` alongside the index and name. Index and
  name are display conveniences that shift as desktops are added or removed;
  the GUID is what the API actually keys on, so callers can pass it straight
  back in ``target``.

Everything the caller sees — the tool description, every error, every note in
a result — is written in Chinese, because that is the language the person and
the model using this server work in. Identifiers and JSON keys stay ASCII.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

from fastmcp import Context
from mcp.types import ToolAnnotations

from windows_mcp.infrastructure import with_analytics

logger = logging.getLogger(__name__)

# One STA thread for every COM call this tool makes. See the module docstring.
_vdm_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="windows-mcp-vdm")
_com = threading.local()


def _ensure_apartment() -> None:
    """CoInitialize this thread as STA exactly once.

    ``vdm.core`` calls CoInitialize itself, but swallows the result, so a thread
    that is already MTA would keep calling into an STA-only object. Doing it
    here first makes the apartment this tool's decision rather than an accident.
    """
    if getattr(_com, "ready", False):
        return
    # COINIT_APARTMENTTHREADED = 0x2. S_OK / S_FALSE both mean usable;
    # RPC_E_CHANGED_MODE (0x80010106) means someone already chose MTA.
    hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2)
    _com.ready = True
    if hr not in (0, 1):
        logger.warning("VirtualDesktop: CoInitializeEx 返回 0x%08X", hr & 0xFFFFFFFF)


def _on_vdm_thread(func, *args, timeout: float = 20, **kwargs):
    """Run ``func`` on the dedicated STA thread and return its result."""

    def _run():
        _ensure_apartment()
        return func(*args, **kwargs)

    return _vdm_executor.submit(_run).result(timeout=timeout)


def _manager():
    """The per-thread VirtualDesktopManager, dropped after a COM failure.

    ``vdm.core`` caches the manager in a thread-local. When explorer.exe
    restarts, that proxy is dead forever; ``_forget_manager`` is the documented
    way to force a fresh one on the next call.
    """
    from windows_mcp.vdm import core

    return core._get_manager()


def _forget_manager() -> None:
    from windows_mcp.vdm import core

    core._forget_manager()


# --------------------------------------------------------------- raw readers


def _internal():
    mgr = _manager()
    if mgr._internal_manager is None:
        raise RuntimeError(
            "本机这个 Windows 版本（build "
            f"{_build()}）没有暴露 IVirtualDesktopManagerInternal，无法管理虚拟桌面。"
        )
    return mgr._internal_manager


def _build() -> int:
    import sys

    return sys.getwindowsversion().build


def _current_pid() -> int:
    import os

    return os.getpid()


def _desktop_id(desktop) -> str:
    return str(desktop.GetID())


def _read_desktops() -> list[dict[str, Any]]:
    """Snapshot every desktop, in taskbar order, with the current one flagged."""
    from windows_mcp.vdm import core

    internal = _internal()
    mgr = _manager()

    current_id = _desktop_id(internal.GetCurrentDesktop())
    array = internal.GetDesktops()
    total = array.GetCount()

    desktops: list[dict[str, Any]] = []
    for index in range(total):
        unknown = array.GetAt(index, ctypes.byref(core.IVirtualDesktop._iid_))
        desktop = unknown.QueryInterface(core.IVirtualDesktop)
        desktop_id = _desktop_id(desktop)
        # The name lives in the registry, not in COM; unnamed desktops have no
        # value at all and are addressed by position instead.
        name = mgr._get_name_from_registry(desktop_id)
        desktops.append(
            {
                "index": index,
                "desktop_id": desktop_id,
                "name": name,
                "current": desktop_id == current_id,
            }
        )
    return desktops


def _resolve(target: str | int) -> dict[str, Any]:
    """Resolve a target to one desktop record.

    Accepts the GUID, the exact registry name, a 1-based position, or
    ``current``. Ambiguous names raise instead of picking one: silently moving
    a window to the wrong group is worse than making the caller be specific.
    """
    desktops = _read_desktops()
    if not desktops:
        raise RuntimeError("系统没有报告任何虚拟桌面。")

    if isinstance(target, int) or (isinstance(target, str) and target.strip().isdigit()):
        position = int(target)
        if not 1 <= position <= len(desktops):
            raise ValueError(
                f"桌面序号 {position} 超出范围：当前一共 {len(desktops)} 个桌面，序号从 1 开始。"
            )
        return desktops[position - 1]

    text = str(target).strip()
    if not text:
        raise ValueError("target 不能为空：请给桌面编号（从 1 开始）、桌面 ID、桌面名称，或 current。")
    if text.casefold() == "current":
        for desktop in desktops:
            if desktop["current"]:
                return desktop
        raise RuntimeError("系统没有标记任何桌面为当前桌面，状态异常。")

    for desktop in desktops:
        if desktop["desktop_id"].casefold() == text.casefold():
            return desktop

    matches = [d for d in desktops if (d["name"] or "").casefold() == text.casefold()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ids = "、".join(d["desktop_id"] for d in matches)
        raise ValueError(
            f"有 {len(matches)} 个桌面都叫「{text}」，无法确定是哪一个；请改用其中一个 ID：{ids}"
        )

    known = "、".join(f"{d['index'] + 1}={d['name'] or '未命名'}" for d in desktops)
    raise ValueError(f"找不到与 {target!r} 匹配的桌面。现有桌面：{known}")


def _find_desktop_object(desktop_id: str):
    from comtypes import GUID

    return _internal().FindDesktop(GUID(desktop_id))


def _supports_naming() -> bool:
    """Whether this build's internal manager exposes SetName.

    Naming arrived with the Windows 11 shell (build 22000+); Windows 10's
    IVirtualDesktopManagerInternal vtable ends before it. The check is on the
    bound interface rather than the build number because the build-number
    branches in ``vdm.core`` are keyed to IIDs, not to method presence.
    """
    try:
        return hasattr(_internal(), "SetName")
    except Exception:
        return False


def _require_naming() -> None:
    if not _supports_naming():
        raise NotImplementedError(
            f"桌面命名是 Windows 11 才有的能力，本机是 build {_build()}，没有这个方法。"
            "请改用桌面 ID 或从 1 开始的序号来指定桌面。"
        )


# ------------------------------------------------------------------ windows


def _window_desktop_map() -> dict[int, str]:
    """Map HWND to desktop id for every top-level window the shell tracks."""
    import win32gui

    mgr = _manager()
    mapping: dict[int, str] = {}

    def _collect(hwnd, _):
        try:
            desktop_id = mgr.get_window_desktop_id(hwnd)
        except Exception:
            return True
        if desktop_id:
            mapping[hwnd] = desktop_id
        return True

    win32gui.EnumWindows(_collect, None)
    return mapping


def _describe_windows(desktop_id: str | None = None) -> list[dict[str, Any]]:
    import win32gui
    import win32process

    mapping = _window_desktop_map()
    rows: list[dict[str, Any]] = []
    for hwnd, window_desktop_id in mapping.items():
        if desktop_id and window_desktop_id.casefold() != desktop_id.casefold():
            continue
        title = win32gui.GetWindowText(hwnd)
        if not title or not win32gui.IsWindowVisible(hwnd):
            continue
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        rows.append(
            {
                "handle": hex(hwnd),
                "title": title,
                "process_id": pid,
                "desktop_id": window_desktop_id,
            }
        )
    return rows


def _resolve_window(handle: int | str | None, title: str | None) -> int:
    """Pick the window to move: explicit handle, title substring, else foreground."""
    import win32gui

    if handle is not None:
        hwnd = int(str(handle), 0)
        if not win32gui.IsWindow(hwnd):
            raise ValueError(f"{handle!r} 不是一个有效的窗口句柄。")
        return hwnd

    if title:
        needle = title.casefold()
        hits: list[int] = []

        def _scan(hwnd, _):
            if win32gui.IsWindowVisible(hwnd) and needle in win32gui.GetWindowText(hwnd).casefold():
                hits.append(hwnd)
            return True

        win32gui.EnumWindows(_scan, None)
        if not hits:
            raise ValueError(f"没有可见窗口的标题包含「{title}」。")
        if len(hits) > 1:
            listed = "；".join(
                f"{hex(h)} {win32gui.GetWindowText(h)[:60]}" for h in hits[:8]
            )
            raise ValueError(
                f"有 {len(hits)} 个窗口都匹配「{title}」，请改用 handle 指定。匹配到：{listed}"
            )
        return hits[0]

    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        raise RuntimeError("当前没有前台窗口可搬；请用 handle 或 title 指定窗口。")
    return hwnd


def _move_window(hwnd: int, desktop_id: str) -> None:
    """Move one window, translating the API's most misleading failure.

    ``IVirtualDesktopManager::MoveWindowToDesktop`` returns E_ACCESSDENIED for a
    window this process does not own on Windows 10 19045 (verified on this
    machine: our own window moves fine, notepad and the DSH window both fail
    with 0x80070005). It is not an access-control problem — the token is the
    same, elevation is irrelevant, and the documented alternatives do not apply
    here either: ``IApplicationViewCollection`` is not served by ImmersiveShell
    on this build (QueryService → E_NOTIMPL for both the CLSID and IID service
    keys), and the shell's Shift+Win+Arrow accelerator ignores synthesized
    input. Attempting the move first and translating the failure keeps Windows
    11 working, where the same call is accepted, while naming the Windows 10
    limitation instead of surfacing a bare Win32 code that reads like a
    permissions bug.
    """
    import win32process

    from comtypes import GUID

    try:
        _manager()._manager.MoveWindowToDesktop(hwnd, ctypes.byref(GUID(desktop_id)))
        return
    except Exception as exc:
        code = getattr(exc, "hresult", None)
        refused = isinstance(code, int) and (code & 0xFFFFFFFF) == 0x80070005
        _, owner_pid = win32process.GetWindowThreadProcessId(hwnd)
        if not refused or owner_pid == _current_pid():
            raise
        raise PermissionError(
            f"窗口 {hex(hwnd)} 属于进程 {owner_pid}。本机（build {_build()}）的 "
            "MoveWindowToDesktop 只允许搬动本进程自己的窗口，搬别的进程的窗口会直接返回 "
            "E_ACCESSDENIED，而且这个版本上没有任何可用的绕行办法。什么都没改。"
            "替代做法：不动这个窗口，直接在它所在的桌面上操作（Snapshot/Click）；"
            "或者让服务进程自己启动并持有这个窗口。"
        ) from exc


# ------------------------------------------------------------------- actions


def _do_list() -> dict[str, Any]:
    desktops = _read_desktops()
    naming = _supports_naming()
    return {
        "build": _build(),
        "count": len(desktops),
        "desktops": desktops,
        "naming": naming,
        "note": (
            "index 从 1 开始，用于指定位次；desktop_id 在整个过程中稳定，重排也不会变。"
            if naming
            else "index 从 1 开始，用于指定位次；desktop_id 稳定，重排也不会变。"
            "本机这个 Windows 版本不支持给桌面命名，name 永远是 null，"
            "请用 desktop_id 或 index 指定桌面。"
        ),
    }


def _do_current() -> dict[str, Any]:
    for desktop in _read_desktops():
        if desktop["current"]:
            return {"current": desktop, "build": _build()}
    raise RuntimeError("系统没有报告当前桌面。")


def _do_create(name: str | None) -> dict[str, Any]:
    from windows_mcp.vdm import core

    # Check the capability before touching anything. Creating an unnamed desktop
    # and then admitting the name was dropped would leave the caller with a
    # desktop it did not ask for.
    if name:
        _require_naming()

    internal = _internal()
    before = {d["desktop_id"] for d in _read_desktops()}

    desktop = internal.CreateDesktopW()
    created_id = _desktop_id(desktop)

    if name:
        handle = core.create_hstring(name)
        try:
            internal.SetName(desktop, handle)
        finally:
            core.delete_hstring(handle)

    after = _read_desktops()
    created = next((d for d in after if d["desktop_id"] == created_id), None)
    if created is None:
        raise RuntimeError(
            "系统接受了 CreateDesktopW，但新建的桌面没有出现在 GetDesktops() 里；"
            "没有任何改动生效。"
        )

    result: dict[str, Any] = {
        "created": created,
        "desktops": after,
        "appeared": len(after) - len(before),
        # Reported from the re-read, not assumed: Windows 11 activates a new
        # desktop, Windows 10 leaves you where you were. A caller that needs to
        # be on the new one should call switch and not trust this flag.
        "switched": created["current"],
        "note": (
            "新桌面已经变成当前桌面。"
            if created["current"]
            else "新桌面建好了，但系统仍停在原来那个上；需要切过去的话再调 action='switch'。"
        ),
    }
    if name and (created["name"] or "") != name:
        # SetName was accepted but did not stick; say so instead of implying success.
        result["name_applied"] = False
        result["warning"] = (
            f"系统接受了 SetName，但桌面读回来是「{created['name']}」；"
            "后续请用 desktop_id 或序号指定这个桌面。"
        )
    return result


def _do_switch(target: str | int) -> dict[str, Any]:
    desktop = _resolve(target)
    if desktop["current"]:
        return {"current": desktop, "changed": False, "note": "已经在这个桌面上了，无需切换。"}

    internal = _internal()
    internal.SwitchDesktop(_find_desktop_object(desktop["desktop_id"]))

    after = _do_current()["current"]
    if after["desktop_id"] != desktop["desktop_id"]:
        raise RuntimeError(
            f"SwitchDesktop 没有报错，但当前桌面仍然是 {after['desktop_id']}"
            f"（{after['name'] or '未命名'}），不是期望的 {desktop['desktop_id']}。"
        )
    return {"current": after, "changed": True}


def _do_rename(target: str | int, name: str) -> dict[str, Any]:
    from windows_mcp.vdm import core

    if not name or not name.strip():
        raise ValueError("name 不能为空。")
    _require_naming()

    desktop = _resolve(target)
    internal = _internal()
    handle = core.create_hstring(name)
    try:
        internal.SetName(_find_desktop_object(desktop["desktop_id"]), handle)
    finally:
        core.delete_hstring(handle)

    renamed = _resolve(desktop["desktop_id"])
    if (renamed["name"] or "") != name:
        raise RuntimeError(
            f"SetName 没有报错，但桌面读回来仍然是「{renamed['name']}」；"
            "本机这个 Windows 版本不会保存桌面名称。"
        )
    return {"desktop": renamed, "desktops": _read_desktops()}


def _do_remove(target: str | int) -> dict[str, Any]:
    desktop = _resolve(target)
    desktops = _read_desktops()
    if len(desktops) == 1:
        raise ValueError("不能删除唯一的一个桌面；Windows 至少要求保留一个。")

    internal = _internal()
    victim = _find_desktop_object(desktop["desktop_id"])

    fallback = next(d for d in desktops if d["desktop_id"] != desktop["desktop_id"])
    # Removing the desktop you are on makes the shell pick its own fallback;
    # naming one explicitly keeps the result predictable.
    was_current = desktop["current"]
    internal.RemoveDesktop(victim, _find_desktop_object(fallback["desktop_id"]))

    after = _read_desktops()
    if any(d["desktop_id"] == desktop["desktop_id"] for d in after):
        raise RuntimeError(
            f"RemoveDesktop 没有报错，但 {desktop['desktop_id']} 仍然在列表里。"
        )

    current = _do_current()["current"]
    return {
        "removed": desktop,
        "was_current": was_current,
        "current": current,
        "desktops": after,
        "note": "系统把这个桌面上的窗口收拢到了另一个桌面，不是合并过去的。",
    }


def _do_windows(target: str | int | None) -> dict[str, Any]:
    if target is None:
        desktops = _read_desktops()
        rows = _describe_windows()
        by_id = {d["desktop_id"]: d for d in desktops}
        for row in rows:
            desktop = by_id.get(row["desktop_id"])
            row["desktop_index"] = desktop["index"] + 1 if desktop else None
            row["desktop_name"] = desktop["name"] if desktop else None
        return {"windows": rows, "count": len(rows)}

    desktop = _resolve(target)
    rows = _describe_windows(desktop["desktop_id"])
    return {"desktop": desktop, "windows": rows, "count": len(rows)}


def _do_move_window(
    target: str | int,
    handle: int | str | None,
    title: str | None,
) -> dict[str, Any]:
    desktop = _resolve(target)
    hwnd = _resolve_window(handle, title)

    import win32gui
    import win32process

    window_title = win32gui.GetWindowText(hwnd)
    _, owner_pid = win32process.GetWindowThreadProcessId(hwnd)
    before = _manager().get_window_desktop_id(hwnd)

    if before.casefold() == desktop["desktop_id"].casefold():
        return {
            "window": {"handle": hex(hwnd), "title": window_title, "process_id": owner_pid},
            "desktop": desktop,
            "changed": False,
            "note": "这个窗口已经在该桌面上了。",
        }

    _move_window(hwnd, desktop["desktop_id"])

    after = _manager().get_window_desktop_id(hwnd)
    if after.casefold() != desktop["desktop_id"].casefold():
        raise RuntimeError(
            f"MoveWindowToDesktop 没有报错，但窗口现在在 {after or '未知桌面'}，"
            f"不是期望的 {desktop['desktop_id']}。"
        )
    return {
        "window": {"handle": hex(hwnd), "title": window_title, "process_id": owner_pid},
        "desktop": desktop,
        "changed": True,
        "previous_desktop_id": before,
    }


def _dispatch(action: str, *, target=None, name=None, handle=None, title=None) -> dict[str, Any]:
    """Route one action. Keyword-only after ``action`` on purpose: the five
    arguments are four strings/ints that mean different things per action, and
    positional calls silently swap them (a test harness did exactly that with
    ``handle`` and ``name`` before this was enforced)."""

    def _run() -> dict[str, Any]:
        if action == "list":
            return _do_list()
        if action == "current":
            return _do_current()
        if action == "windows":
            return _do_windows(target)
        if action == "create":
            return _do_create(name)
        if action == "switch":
            return _do_switch(_require_target(action, target))
        if action == "rename":
            return _do_rename(_require_target(action, target), _require_name(action, name))
        if action == "remove":
            return _do_remove(_require_target(action, target))
        if action == "move_window":
            return _do_move_window(_require_target(action, target), handle, title)
        raise ValueError(f"没有这个 action：{action!r}。")

    try:
        return _on_vdm_thread(_run)
    except Exception as exc:
        # A dead proxy is indistinguishable from a real COM error at the call
        # site, and explorer.exe restarts are exactly when this happens. Drop
        # the cached manager so the next call is judged on its own merits.
        if "0x800706BA" in repr(exc) or "RPC server is unavailable" in str(exc):
            _forget_manager()
        raise


def _require_target(action: str, target) -> Any:
    if target is None or (isinstance(target, str) and not target.strip()):
        raise ValueError(f"{action} 需要 target：桌面 ID、桌面名称，或从 1 开始的序号。")
    return target


def _require_name(action: str, name: str | None) -> str:
    if not name or not name.strip():
        raise ValueError(f"{action} 需要 name。")
    return name.strip()


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="VirtualDesktop",
        description=(
            "管理 Windows 10/11 虚拟桌面（任务栏上那几组窗口）。"
            "关键词：虚拟桌面、工作区、新建桌面、切换桌面、把窗口移到桌面。"
            "action='list' 返回所有桌面，每个都带稳定的 desktop_id、从 1 开始的 index，以及是否为当前桌面。"
            "action='current' 只返回当前桌面。"
            "action='windows' 列出可见的顶层窗口并按桌面分组（给 target 就只列那个桌面的）。"
            "action='create' 新建一个桌面；只有在系统支持时才会接受 name。"
            "action='switch' 把整个桌面切到 target。"
            "action='rename' 给 target 设一个显示名称。"
            "action='remove' 关闭一个桌面，Windows 会把上面的窗口收拢到另一个桌面（不是合并）。"
            "action='move_window' 把一个窗口移到 target，窗口由 handle、title 片段指定，"
            "都不给就移动前台窗口。它只能移动本服务进程自己的窗口："
            "Windows 10 对其它进程的窗口一律返回拒绝访问，而本版本没有可用的替代路线，"
            "所以调用会失败并说明原因，且不做任何改动。"
            "target 可以是 desktop_id、精确的桌面名称、从 1 开始的序号，或者字面量 current。"
            "桌面命名需要 Windows 11；在 Windows 10 上所有 name 都是 null，请用 desktop_id 或序号指定。"
            "在 Windows 10 上新建桌面不会自动切过去（Windows 11 会），返回结果里会如实说明实际发生了什么。"
            "这个工具只做窗口分组，不隔离输入、焦点和鼠标：切走的桌面仍在运行，"
            "坐在键盘前的人用的还是同一个指针。每次改动都会重新读取系统状态，确认之后才报告成功。"
        ),
        annotations=ToolAnnotations(
            title="虚拟桌面",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "VirtualDesktop-Tool")
    def virtual_desktop_tool(
        action: Literal[
            "list",
            "current",
            "windows",
            "create",
            "switch",
            "rename",
            "remove",
            "move_window",
        ],
        target: str | int | None = None,
        name: str | None = None,
        handle: int | str | None = None,
        title: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        # Reads ignore the mutation arguments instead of rejecting them: a model
        # that carries `target` over from a previous call should still get the
        # listing it asked for.
        return _dispatch(action, target=target, name=name, handle=handle, title=title)

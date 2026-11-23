"""Programs tool — launch/arrange windows and manage processes.

原来是 App（launch / launch_executable / resize / switch）和 Process（list / kill）
两个工具。它们都围绕"这台机器上跑着什么"，参数也不冲突（App 用 name/window_loc/
window_size/executable/args/cwd，Process 用 name/pid/sort_by/limit/force），
合并后共用一个 mode 调度器。

`name` 是唯一的重叠参数，而且语义不同：
    launch / resize / switch  → 窗口或开始菜单里的程序名
    list / kill               → 进程名
两者都是"按名字找东西"，调用方不需要区分，所以共用一个不带前缀的参数。
真正需要防的是**该用 pid 的地方用成 name**、或反过来 —— 下面按 mode 校验。
"""

import json
import subprocess
from pathlib import Path
from typing import Literal

from mcp.types import ToolAnnotations
from windows_mcp.desktop.utils import as_bool as _as_bool
from windows_mcp.desktop.utils import as_int_list as _as_int_list
from windows_mcp.infrastructure import with_analytics
from fastmcp import Context
from windows_mcp import process


def _as_args(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        args = value
    else:
        args = json.loads(value)
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError("args must be a list of strings")
    return args


def _resolve_executable(executable: str) -> Path:
    path = Path(executable).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Executable does not exist: {path}")
    return path


def _resolve_cwd(cwd: str | None) -> Path | None:
    if cwd is None:
        return None
    path = Path(cwd).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Working directory does not exist: {path}")
    return path


def _launch_executable(
    executable: str,
    args: list[str] | str | None,
    cwd: str | None,
) -> str:
    resolved_executable = _resolve_executable(executable)
    resolved_cwd = _resolve_cwd(cwd)
    resolved_args = _as_args(args)

    process_handle = subprocess.Popen(
        [str(resolved_executable), *resolved_args],
        cwd=str(resolved_cwd) if resolved_cwd is not None else None,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return json.dumps(
        {
            "pid": process_handle.pid,
            "executable": str(resolved_executable),
            "args": resolved_args,
            "cwd": str(resolved_cwd) if resolved_cwd is not None else None,
        },
        indent=2,
    )


_WINDOW_MODES = ("launch", "launch_executable", "resize", "switch")
_PROCESS_MODES = ("list", "kill")
_ALL_MODES = _WINDOW_MODES + _PROCESS_MODES

# 每个 mode 接受的参数，用于拒绝跨 mode 的误传。
# 不做静默忽略：把 pid 传给 launch 会被当成"没传"，于是启动一个名字为空的东西，
# 比直接报错难查得多。
_MODE_PARAMS = {
    "launch": ("name", "window_loc", "window_size"),
    "launch_executable": ("executable", "args", "cwd"),
    "resize": ("name", "window_loc", "window_size"),
    "switch": ("name",),
    "list": ("name", "sort_by", "limit"),
    "kill": ("name", "pid", "force"),
}


def _check_mode_params(mode: str, supplied: dict[str, object]) -> None:
    """Reject parameters this mode does not accept.

    *supplied* must already hold only what the caller actually chose: values that
    merely equal a default are dropped by the caller before calling this, so a
    default can never be mistaken for deliberate input.
    """
    allowed = _MODE_PARAMS[mode]
    rejected = [key for key, value in supplied.items() if value is not None and key not in allowed]
    if rejected:
        raise ValueError(
            f"mode={mode!r} 不接受参数 {', '.join(sorted(rejected))}。"
            f"该 mode 可用：{', '.join(allowed)}。"
        )


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="Programs",
        description=(
            "启动程序、管理窗口、查杀进程，一个入口。Keywords: open, start, launch, program, "
            "application, window, foreground, focus, resize, task manager, running tasks, kill, "
            "terminate, stop process, PID, CPU, memory usage. "
            "Six modes: "
            "'launch' opens an application by Start Menu name (name); "
            "'launch_executable' strictly launches one executable path with separated argv and "
            "optional cwd (executable, args, cwd); "
            "'resize' adjusts a named or active window (name, window_loc, window_size); "
            "'switch' brings a specific window into focus (name); "
            "'list' lists running processes with filtering and sorting (name, sort_by, limit) — "
            "sort_by is memory/cpu/name; "
            "'kill' terminates processes by exact pid or by name (pid, force) — prefer an exact pid. "
            "Parameters belonging to another mode are rejected rather than ignored. "
            "Prefer background execution when supported. If launching an application or terminating "
            "a process is known to take focus or disrupt the user, obtain permission first, reusing "
            "existing authorization. Relaxed policy does not gate these modes; this guidance is not "
            "server-enforced approval."
        ),
        annotations=ToolAnnotations(
            title="Programs",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Programs-Tool", run_sync_in_thread=False)
    def programs_tool(
        mode: Literal["launch", "launch_executable", "resize", "switch", "list", "kill"] = "list",
        name: str | None = None,
        window_loc: list[int] | str | None = None,
        window_size: list[int] | str | None = None,
        executable: str | None = None,
        args: list[str] | str | None = None,
        cwd: str | None = None,
        pid: int | None = None,
        sort_by: Literal["memory", "cpu", "name"] = "memory",
        limit: int = 20,
        force: bool | str = False,
        ctx: Context = None,
    ):
        act = (mode or "list").strip().lower()
        if act not in _ALL_MODES:
            raise ValueError(
                f"未知 mode {mode!r}。可用：{' / '.join(_ALL_MODES)}"
            )

        # 只把"和默认值不同"的参数当作显式传入。sort_by/limit 的默认值和
        # "传了默认值"在 Python 签名里无法区分，直接按默认值过滤 —— 代价是
        # 有人显式传 sort_by="memory" 到 list 以外会被忽略，这个误判是安全的
        # （等于没传），而反过来把默认值当误传去报错会拦住正常调用。
        supplied = {
            "name": name,
            "window_loc": window_loc,
            "window_size": window_size,
            "executable": executable,
            "args": args,
            "cwd": cwd,
            "pid": pid,
            "sort_by": sort_by if sort_by != "memory" else None,
            "limit": limit if limit != 20 else None,
            "force": True if _as_bool(force, "force") else None,
        }
        _check_mode_params(act, supplied)

        if act in _WINDOW_MODES:
            if act == "launch_executable":
                if executable is None:
                    raise ValueError('mode="launch_executable" 需要 executable')
                return _launch_executable(executable, args, cwd)

            window_loc = _as_int_list(window_loc, "window_loc")
            window_size = _as_int_list(window_size, "window_size")
            return get_desktop().app(act, name, window_loc, window_size)

        if act == "list":
            return process.list_processes(name=name, sort_by=sort_by, limit=limit)

        # kill
        return process.kill_process(name=name, pid=pid, force=_as_bool(force, "force"))

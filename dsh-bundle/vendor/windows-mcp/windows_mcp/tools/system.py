"""System tool — shell, files, and registry in one entry.

原来是 PowerShell、FileSystem、Registry 三个工具。三者都是"直接读写这台机器"，
安全注解完全一致（readOnlyHint=False / destructiveHint=True），合并后注解语义仍然正确。

mode 分三组，每组一个前缀，避免 mode 名撞车：

    文件    read / write / copy / move / delete / list / search / info
    注册表  reg_get / reg_set / reg_delete / reg_list
    命令    shell

注册表带 `reg_` 前缀是必须的：文件组的 read / write / delete / list 和注册表组的
get / set / delete / list 有四个语义相同、目标完全不同的动作。不带前缀的话
`mode="delete"` 到底是删文件还是删注册表键，只能靠 path 长什么样去猜 —— 猜错就是
删错东西。加前缀之后 mode 自己就说明了目标。

`path` 是唯一的重叠参数：文件模式是文件/目录路径，注册表模式是注册表键路径。
同一个名字、同一个字符串类型、完全不同的命名空间，这是本工具最危险的地方，
下面按 mode 校验并给出明确的错误信息，绝不做静默忽略。
"""

import os
from typing import Literal

from mcp.types import ToolAnnotations
from windows_mcp.desktop.utils import as_bool as _as_bool
from windows_mcp.infrastructure import with_analytics
from windows_mcp import filesystem
from windows_mcp import registry
from windows_mcp.registry import RegistryType
from windows_mcp.powershell import COMMAND_TIMED_OUT, PowerShellExecutor
from windows_mcp.tools._shell_progress import run_with_progress
from fastmcp import Context


def cap_output(text: str, max_output: int) -> str:
    """Keep the head and the tail of *text* when it exceeds *max_output* chars.

    MCP clients cap the size of a tool result and pass none of it to the model
    when the cap is exceeded, so a command that printed hundreds of
    kilobytes returned nothing usable. Keeping both ends preserves the leading
    progress and the trailing errors and summary, where the useful information
    usually lives — the tail gets the larger share.
    """
    if max_output <= 0 or len(text) <= max_output:
        return text
    if max_output < 5:
        return text[:max_output]

    def make_marker(omitted: int) -> str:
        return (
            f"\n...[Output truncated: {omitted:,} of {len(text):,} characters omitted. "
            "Re-run the command writing its output to a file (for example "
            "'... | Out-File -Encoding utf8 <path>') and read that file in parts with "
            "mode='read' using offset/limit, or raise max_output (0 disables "
            "capping).]...\n"
        )

    # Using the largest possible omission count gives an upper bound on the
    # marker length; the final marker can only be shorter.
    marker = make_marker(len(text))
    if len(marker) + 2 > max_output:
        marker = "..."
    budget = max_output - len(marker)
    head = max(budget * 2 // 5, 1)
    tail = budget - head
    if marker != "...":
        marker = make_marker(len(text) - head - tail)
    return text[:head] + marker + text[-tail:]


def _resolve_path(path: str, default_dir: str) -> str:
    """Expand a leading ~ to the home folder; resolve other relative paths from *default_dir*."""
    # Only '~' alone or followed by a separator, as in PowerShell. expanduser() also
    # expands '~name', which would turn '~$report.docx' (an Office lock file) into a
    # '$report.docx' next to the profile folder.
    if path == '~' or path.startswith(('~/', '~\\')):
        path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(default_dir, path)
    return path


_FILE_MODES = ('read', 'write', 'copy', 'move', 'delete', 'list', 'search', 'info')
_REG_MODES = ('reg_get', 'reg_set', 'reg_delete', 'reg_list')
_ALL_MODES = _FILE_MODES + _REG_MODES + ('shell',)

# 每个 mode 接受的参数。跨 mode 误传直接报错 ——
# 尤其 path：把一个注册表键路径当成文件路径去 write，会在桌面下建出一个叫
# "HKCU:\Software\..." 的怪异文件；反过来更糟。静默忽略参数只会让这种错误更难查。
#
# 判定标准是**意图**，不是"上游签名里有没有"：
#   - content 出现在 list/delete 里 → 调用方以为自己在写，其实在列/删 → 必须报错
#   - overwrite 出现在 write 里 → 调用方明确表达"要覆盖"，而 write 本来就是覆盖写
#     → 语义一致，接受并忽略
# 只按上游签名裁剪会把第二种也拦下来，那是给用户添堵，不是防错。
_MODE_PARAMS = {
    'read': ('path', 'offset', 'limit', 'encoding'),
    'write': ('path', 'content', 'append', 'overwrite', 'encoding'),
    'copy': ('path', 'destination', 'overwrite'),
    'move': ('path', 'destination', 'overwrite'),
    'delete': ('path', 'recursive'),
    'list': ('path', 'pattern', 'recursive', 'show_hidden'),
    'search': ('path', 'pattern', 'recursive'),
    'info': ('path', 'show_hidden'),
    'reg_get': ('path', 'name'),
    'reg_set': ('path', 'name', 'value', 'type'),
    'reg_delete': ('path', 'name'),
    'reg_list': ('path', 'show_hidden'),
    'shell': ('command', 'timeout', 'max_output'),
}


def _check_mode_params(mode: str, supplied: dict[str, object]) -> None:
    allowed = _MODE_PARAMS[mode]
    rejected = [key for key, value in supplied.items() if value is not None and key not in allowed]
    if rejected:
        raise ValueError(
            f"mode={mode!r} 不接受参数 {', '.join(sorted(rejected))}。"
            f"该 mode 可用：{', '.join(allowed)}。"
        )


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name='System',
        description=(
            "直接读写这台机器：执行命令、操作文件、读写注册表。Keywords: shell, run, execute, cmd, "
            "terminal, command line, script, powershell, file, directory, folder, read, write, copy, "
            "move, delete, search, regedit, registry, HKEY, HKCU, HKLM, Windows settings. "
            "Modes are grouped by target — "
            "files: 'read' (offset/limit line range), 'write' (append=True to append), 'copy', 'move', "
            "'delete' (recursive=True for non-empty dirs), 'list' (pattern filter, show_hidden), "
            "'search' (glob pattern), 'info' (size/dates/type); relative file paths resolve from the "
            "user's Desktop folder, use absolute paths elsewhere — "
            "registry: 'reg_get' (returns the value with its type), 'reg_set' (create/update), "
            "'reg_delete' (removes a value, or the key when name is omitted), 'reg_list' (values and "
            "sub-keys); registry paths use PowerShell format like \"HKCU:\\Software\\MyApp\"; for "
            "type=\"Binary\" value lists bytes as decimal or 0x hex separated by commas or spaces, or "
            ".reg notation (\"hex:01,02,ff\"); for type=\"MultiString\" put each string on its own line — "
            "shell: 'shell' runs any PowerShell command (command/timeout/max_output); a command ended by "
            "the timeout reports Status Code: timed out and includes whatever it printed; output longer "
            "than max_output is cut to head and tail with a marker reporting how much was omitted. "
            "The 'path' argument means a filesystem path in file modes and a registry key path in reg_ "
            "modes; parameters belonging to another mode are rejected rather than ignored. "
            "Under relaxed control policy this tool is not desktop-gated. Prefer background execution; "
            "coordinate with the user before commands known to interfere with their desktop, reusing "
            "existing authorization."
        ),
        annotations=ToolAnnotations(
            title="System",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    @with_analytics(get_analytics(), "System-Tool", run_sync_in_thread=False)
    async def system_tool(
        mode: Literal[
            'read', 'write', 'copy', 'move', 'delete', 'list', 'search', 'info',
            'reg_get', 'reg_set', 'reg_delete', 'reg_list',
            'shell',
        ],
        path: str | None = None,
        destination: str | None = None,
        content: str | None = None,
        pattern: str | None = None,
        recursive: bool | str = False,
        append: bool | str = False,
        overwrite: bool | str = False,
        offset: int | None = None,
        limit: int | None = None,
        encoding: str = 'utf-8',
        show_hidden: bool | str = False,
        name: str | None = None,
        value: str | None = None,
        type: RegistryType = 'String',
        command: str | None = None,
        timeout: int = 30,
        max_output: int = 20000,
        ctx: Context = None,
    ):
        act = (mode or '').strip().lower()
        if act not in _ALL_MODES:
            raise ValueError(f"未知 mode {mode!r}。可用：{' / '.join(_ALL_MODES)}")

        supplied = {
            'path': path,
            'destination': destination,
            'content': content,
            'pattern': pattern,
            'recursive': True if _as_bool(recursive, 'recursive') else None,
            'append': True if _as_bool(append, 'append') else None,
            'overwrite': True if _as_bool(overwrite, 'overwrite') else None,
            'offset': offset,
            'limit': limit,
            'encoding': encoding if encoding != 'utf-8' else None,
            'show_hidden': True if _as_bool(show_hidden, 'show_hidden') else None,
            'name': name,
            'value': value,
            'type': type if type != 'String' else None,
            'command': command,
            'timeout': timeout if timeout != 30 else None,
            'max_output': max_output if max_output != 20000 else None,
        }
        _check_mode_params(act, supplied)

        # ---------------------------------------------------------------- shell
        if act == 'shell':
            if not command:
                raise ValueError('mode="shell" 需要 command')
            # Async so progress notifications go out from the event loop while the
            # command blocks a worker thread; without a progressToken this is the
            # same worker-thread call.
            response, status_code = await run_with_progress(
                ctx, PowerShellExecutor.execute_command, command, timeout, timeout=timeout
            )
            status_text = "timed out" if status_code == COMMAND_TIMED_OUT else status_code
            return f"Response: {cap_output(response, max_output)}\nStatus Code: {status_text}"

        if path is None:
            # 两组都必填 path，但报错信息要指明是哪一组，否则用户不知道 path 该长什么样。
            hint = ('注册表键路径，例如 HKCU:\\Software\\MyApp'
                    if act in _REG_MODES else '文件或目录路径；相对路径从桌面解析')
            raise ValueError(f'mode={act!r} 需要 path（{hint}）')

        # ------------------------------------------------------------- registry
        if act == 'reg_get':
            if name is None:
                raise ValueError('mode="reg_get" 需要 name（要读取的值名）')
            return registry.get_value(path=path, name=name)

        if act == 'reg_set':
            if name is None:
                raise ValueError('mode="reg_set" 需要 name（要写入的值名）')
            if value is None:
                raise ValueError('mode="reg_set" 需要 value')
            return registry.set_value(path=path, name=name, value=value, reg_type=type)

        if act == 'reg_delete':
            return registry.delete_entry(path=path, name=name)

        if act == 'reg_list':
            return registry.list_key(path=path)

        # ------------------------------------------------------------ filesystem
        from platformdirs import user_desktop_dir
        default_dir = user_desktop_dir()
        path = _resolve_path(path, default_dir)
        if destination:
            destination = _resolve_path(destination, default_dir)

        recursive = _as_bool(recursive, 'recursive')
        append = _as_bool(append, 'append')
        overwrite = _as_bool(overwrite, 'overwrite')
        show_hidden = _as_bool(show_hidden, 'show_hidden')

        if act == 'read':
            return filesystem.read_file(path, offset=offset, limit=limit, encoding=encoding)
        if act == 'write':
            if content is None:
                raise ValueError('mode="write" 需要 content')
            return filesystem.write_file(path, content, append=append, encoding=encoding)
        if act == 'copy':
            if destination is None:
                raise ValueError('mode="copy" 需要 destination')
            return filesystem.copy_path(path, destination, overwrite=overwrite)
        if act == 'move':
            if destination is None:
                raise ValueError('mode="move" 需要 destination')
            return filesystem.move_path(path, destination, overwrite=overwrite)
        if act == 'delete':
            return filesystem.delete_path(path, recursive=recursive)
        if act == 'list':
            return filesystem.list_directory(
                path, pattern=pattern, recursive=recursive, show_hidden=show_hidden
            )
        if act == 'search':
            if pattern is None:
                raise ValueError('mode="search" 需要 pattern')
            return filesystem.search_files(path, pattern, recursive=recursive)

        # act == 'info'
        return filesystem.get_file_info(path)

"""tools subpackage — registers all MCP tool definitions on a FastMCP instance.

8 个工具。2026-11 这轮从 15 个合并而来：

    Snapshot + Screenshot + DisplayInventory  ->  Screen
    App + Process                             ->  Programs
    PowerShell + FileSystem + Registry        ->  System
    Clipboard                                 ->  Input 的 clipboard_get / clipboard_set

合并的理由是**参数包含关系**，不是凑数：
  - Screenshot 的六个参数全在 Snapshot 里，DisplayInventory 一个参数都没有；
  - Clipboard 的 text 在 Input 里本来就是参数，Input 内部的 _paste_text 也一直在
    操作剪贴板；
  - App/Process 的 name、System 三个工具的安全注解完全一致。

没合并的三个（Scrape / VirtualDesktop / IsolatedSession）留着的理由写在各自的模块里：
前者要调 LLM，后两者的机制和目标完全不同，合并只会让参数语义变模糊。

旧的模块文件（snapshot.py / display.py / app.py / process.py / shell.py /
filesystem.py / registry.py / clipboard.py）仍然留在目录里，但不再注册 —— 它们的
函数体已经搬进新模块，留着是为了让上游的单元测试还能 import 到符号。
"""

from windows_mcp.tools import (
    control_status,
    input,
    programs,
    screen,
    scrape,
    system,
    virtual_desktop,
    virtual_desktop_session,
)

_MODULES = [
    control_status,
    screen,
    input,
    programs,
    system,
    scrape,
    virtual_desktop,
    virtual_desktop_session,
]


def register_all(mcp, *, get_desktop, get_analytics):
    """Register every tool module on *mcp*.

    *get_desktop* and *get_analytics* are zero-arg callables that return the
    current ``Desktop`` and ``PostHogAnalytics`` instances (resolved lazily so
    that tools can be registered before ``lifespan`` initializes the singletons).
    """
    for mod in _MODULES:
        mod.register(mcp, get_desktop=get_desktop, get_analytics=get_analytics)

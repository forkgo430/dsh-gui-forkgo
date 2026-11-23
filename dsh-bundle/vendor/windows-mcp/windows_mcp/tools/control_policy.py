"""One table for desktop ownership, separate from authorization or data safety.

When adding a tool or a mode, classify it here. Registration coverage tests catch
omissions; unclassified extensions conservatively retain the desktop gate.

2026-11：15 个工具合并成 8 个，这张表必须跟着改，否则会静默出错 ——
不在表里的工具名一律按 `direct` 处理（"未知工具不得绕过控制权"是原有的保守默认），
于是只读的 Screen 也要抢用户的鼠标键盘。这不是崩溃，是行为变差，最容易被漏掉。

新的分类：

    Screen          full/fast 读屏幕 → other；displays 更只是元数据
    Input           全部动作都直接碰桌面 → direct（含 clipboard，它会覆盖用户剪贴板）
    Programs        launch/launch_executable/list → other 或 possible；resize/switch/kill → direct 或 possible
    System          shell → possible；文件与注册表读写 → other/possible，但都要走控制权
    Scrape          只读网页 → other
    VirtualDesktop  按动作分
    IsolatedSession 动的是另一个会话，不抢当前桌面 → other
"""

# Categories describe desktop interference, not whether an operation is destructive.
#
# 合并后 Click/Type/Scroll/Move/Shortcut/MultiSelect/MultiEdit 这些名字不再是工具名，
# 而是 Input 的 action；保留它们在表里没有意义，已并入 Input 的 `direct`。
DIRECT = frozenset({"Input"})
POSSIBLE = frozenset({"Notification"})
OTHER = frozenset({"Screen", "Scrape", "IsolatedSession"})

MODES = {
    # Screen 三个 mode 都不动桌面：截图前会主动把叠加层藏起来再拍。
    "Screen": {"full": "other", "fast": "other", "displays": "other"},
    # Input 的每个 action 都直接动输入设备或系统剪贴板。
    # clipboard_get 严格说只读，但读的是全局共享状态，且紧接着通常就是 set；
    # 单独给它开个例外，收益不抵分类复杂度。
    "Input": {
        "click": "direct",
        "type": "direct",
        "scroll": "direct",
        "move": "direct",
        "shortcut": "direct",
        "multi_select": "direct",
        "multi_edit": "direct",
        "clipboard_get": "direct",
        "clipboard_set": "direct",
        # wait / wait_for 只是等，不碰桌面。但它们在 Input 里，而 Input 的默认分类
        # 是 direct；把这两个降级会让「等条件成立」也被门禁拦住，反而更烦。
        # 保持 direct 是安全的：门禁只会让 AI 多问一次，不会漏掉真正的破坏。
        "wait": "direct",
        "wait_for": "direct",
    },
    "Programs": {
        "launch": "possible",
        "launch_executable": "possible",
        "resize": "direct",
        "switch": "direct",
        "list": "other",
        "kill": "possible",
    },
    "System": {
        # shell 能启动任何东西，包括弹窗、抢占焦点的 GUI。
        "shell": "possible",
        # 文件与注册表：改的是数据不是桌面，但写操作可能打断用户正在做的事。
        "read": "other",
        "info": "other",
        "list": "other",
        "search": "other",
        "write": "possible",
        "copy": "possible",
        "move": "possible",
        "delete": "possible",
        "reg_get": "other",
        "reg_list": "other",
        "reg_set": "possible",
        "reg_delete": "possible",
    },
    "VirtualDesktop": {
        "list": "other",
        "current": "other",
        "windows": "other",
        "rename": "other",
        "create": "direct",
        "switch": "direct",
        "remove": "direct",
        "move_window": "direct",
    },
    "IsolatedSession": {
        # 起停的是另一个会话里的宿主进程，当前桌面上什么都不动。
        "status": "other",
        "setup": "possible",
        "start": "possible",
        "stop": "possible",
        "inject": "possible",
        "forget": "possible",
    },
}
CLASSIFIED_TOOLS = DIRECT | POSSIBLE | OTHER | MODES.keys() | {"ControlStatus"}

# Which argument selects the mode for a tool with several behaviours. VirtualDesktop
# and IsolatedSession say ``action`` instead of ``mode``; leaving this implicit
# would silently classify all of their actions as direct.
MODE_ARGUMENT = {
    "Screen": "mode",
    "Input": "action",
    "Programs": "mode",
    "System": "mode",
    "VirtualDesktop": "action",
    "IsolatedSession": "action",
}

# 每个工具的 mode 参数的默认值。不写的话 get(argument, None) 拿到 None，
# 会导致「没传 mode」直接落进 direct 分支 —— 而 Programs 的默认是 list，
# 那是只读的，不该抢控制权。
MODE_DEFAULT = {
    "Screen": "full",
    "Programs": "list",
    "IsolatedSession": "status",
}


def requires_control(policy: str, name: str, arguments: dict | None = None) -> bool:
    """Decide before taking the ownership lock; never inspect arbitrary scripts."""
    if name == "ControlStatus":
        return False
    if policy == "strict":
        return True
    if policy not in ("relaxed", "balanced"):
        raise ValueError(f"Unknown control policy: {policy}")
    category = "direct"  # Unknown tools/modes must not silently bypass ownership.
    if name in MODES:
        argument = MODE_ARGUMENT.get(name, "mode")
        mode = (arguments or {}).get(argument) or MODE_DEFAULT.get(name)
        if isinstance(mode, str):
            category = MODES[name].get(mode, "direct")
    elif name in POSSIBLE:
        category = "possible"
    elif name in OTHER:
        category = "other"
    return category == "direct" or (policy == "balanced" and category == "possible")

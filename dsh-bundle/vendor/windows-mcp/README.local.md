# windows-mcp 本地分支（DSH）

上游 `windows-mcp==0.8.9` 的原样副本，改了两处：

1. 加了一个工具 `VirtualDesktop`；
2. 屏幕上的控制提示改中文（蓝光边框 + 顶部中间面板 + 接管确认框）。

## 为什么要有这一份

上游把 Windows 虚拟桌面的 COM 封装写好了（`windows_mcp/vdm/core.py`，24 KB，含
`CreateDesktopW` / `SwitchDesktop` / `RemoveDesktop` / `SetName` / `MoveWindowToDesktop`），
但只在一个地方用到它——`desktop/service.py:1416` 的 `is_window_on_current_desktop()`，
用来过滤窗口列表。21 个 MCP 工具里没有一个把它暴露出来。

这份分支把它挂成工具，并修掉上游封装里三个会让工具说谎的毛病。

## 屏幕提示改中文

用户在屏幕上看到的那一圈蓝光、上方中间那行字、以及接管前的确认框，都来自这个包：

| 位置 | 文件 | 原文案 | 现在 |
|---|---|---|---|
| 顶部面板标题 | `desktop/control_overlay_art.py` | `AI is controlling this computer` | **AI 正在控制这台电脑** |
| 接管方式一 | 同上 | `Move the mouse a significant distance` | **大幅移动鼠标** |
| 接管方式二 | 同上 | `or press <快捷键> to take over` | **或按 <快捷键> 接管** |
| 接管确认框 | `desktop/control_consent.py` | `AI is about to take control of this computer` | **AI 即将接管这台电脑** |
| 确认框说明 | 同上 | `If you keep using the computer without answering, you stay in control.` | **你不回应、继续用电脑，控制权就一直在你手上。** |
| 确认框倒计时 | 同上 | `Starting in N s` | **N 秒后开始** |
| 确认框按钮 | 同上 | `Allow now` / `Deny` | **允许** / **拒绝** |

两件事顺带做了：

- **字号调小**。上游 `range(24, 7, -1)` 在 1920 宽的屏上取到 24 → 标题 32px，面板约
  500×160（占屏 26%×15%），像弹窗。现在上限 16 → 标题 24px，面板 398×114
  （占屏 21%×11%），隔一个房间看得见，但不抢视线。
- **换中文字体**。`seguisb.ttf` / `segoeui.ttf` 没有汉字，直接换文案会出一排方框。
  现在按 `msyhbd.ttc → Dengb.ttf → simhei.ttf`（标题）和
  `msyh.ttc → Deng.ttf → simsun.ttc`（正文）依次回退，最后才落到西文字体，
  所以任何一台 Windows 上都不会渲染成豆腐块。确认框那侧用 Tk 的
  `font.families()` 探测，命中 `Microsoft YaHei UI`。

关键词（快捷键名 `Ctrl + Alt + Shift + Backspace`）保持英文——那是键帽上的字，不是翻译对象。

另外修了快捷键黑底的间距：原来 `badge_left + shortcut_width + 16` 紧贴“接管”，只剩 1px；
现在按 `_BADGE_PAD_X` / `_BADGE_GAP` 计算，两侧各留 8px。

## 加了什么

`windows_mcp/tools/virtual_desktop.py` —— 单一工具 `VirtualDesktop`，8 个 action：

| action | 作用 | 控制权分类 |
|---|---|---|
| `list` | 所有桌面：`desktop_id` / 1-based `index` / `name` / `current` | other |
| `current` | 只报当前桌面 | other |
| `windows` | 可见顶层窗口按桌面分组（给 `target` 则只列该桌面） | other |
| `create` | 新建桌面；`name` 只在系统支持时接受 | direct |
| `switch` | 切到 `target` | direct |
| `rename` | 给 `target` 设显示名 | other |
| `remove` | 关闭 `target`，窗口落到另一个桌面（不是合并） | direct |
| `move_window` | 把 `handle`/`title` 指定的窗口（默认前台窗口）挪到 `target` | direct |

`target` 接受 `desktop_id`、精确 `name`、1-based 序号，或字面量 `current`。

## 和上游封装的差异

1. **不信任静默失败。** 上游的 `switch_desktop` / `remove_desktop` / `move_window_to_desktop`
   出错只 `logger.error` 然后 `return None`。工具拿不到失败信号就只能报成功。
   这里每个 mutation 都是「解析 → 直接调 internal manager → 重新读状态 → 对不上就抛」。
2. **单一 STA 线程。** `vdm.core` 的 manager 是 thread-local，而
   `VirtualDesktopManagerInternal` 属于 explorer.exe 的 apartment；MCP 的同步工具体
   默认走 `asyncio.to_thread`，会散落在不同线程上。这里自己持有一个 STA 线程跑全部 COM，
   并且**故意不用 UIA worker**——一次慢 `WaitFor` 不该把切换桌面排在后面。
3. **`create` 的返回名。** 上游 `create_desktop()` 无名字时返回
   `get_all_desktops()[-1]["name"]`，靠「新的排在最后」这个假设；有名字时直接返回入参。
   这里一律按 GUID 回查真实记录。
4. **`remove` 的 fallback。** 上游删当前桌面时靠 Windows 自己挑落点。这里显式指定
   fallback，并在删除后回读确认真的没了。
5. **explorer 重启。** `RPC server is unavailable` / `0x800706BA` 时丢缓存 manager 再抛，
   下一次调用重建，而不是让一个死代理一直报同一个错。

另外改了 `tools/control_policy.py`：`VirtualDesktop` 用 `action` 选行为而不是 `mode`，
新增 `MODE_ARGUMENT` 表把它指对，否则 8 个 action 会被整体当成 `direct`。

## 边界（别误解这个工具）

Windows 虚拟桌面**只隔离窗口，不隔离输入**。切过去以后：

- 鼠标键盘焦点还是共享的，人在键盘前动一下就是同一个指针；
- 切走的桌面上进程照跑，占 CPU、放声音、弹通知；
- 不存在登录会话隔离。

要真正隔离，得用 Terminal Server 子会话（`WTSEnableChildSessions` + `WTSQueryUserToken`），
那是另一个量级的东西，这个工具不碰。

## 实测结果（本机 Windows 10 19045）

| action | 结果 |
|---|---|
| `list` / `current` / `windows` | 正常，含 `desktop_id`、1-based `index`、`current` 标记 |
| `create` | 正常，返回真实 `desktop_id`；**本 build 不自动切换**（Win11 会），`switched:false` 如实上报 |
| `switch` | 正常，按 id / 名字 / 1-based 序号都能命中；已在目标上时 `changed:false` |
| `remove` | 正常，删当前桌时 `was_current:true`，会话退回 fallback |
| `rename` / `create(name=...)` | **拒绝**（`NotImplementedError`）：19045 的 vtable 没有 `SetName`，命名是 Win11 22000+ 的能力；拒绝发生在创建之前，不会留下半个桌面 |
| `move_window`（本进程的窗口） | 正常，搬完在两个桌面的 `windows` 列表里都能对上 |
| `move_window`（别的进程的窗口） | **拒绝**（`PermissionError`，明说“什么都没改”）：`MoveWindowToDesktop` 对非本进程窗口返回 `E_ACCESSDENIED` |

### 关于 `move_window` 的 Windows 10 限制

这不是权限问题——同一 token、同一会话，自己的窗口能搬，notepad 和 DSH 窗口都返回 `0x80070005`。排查过的替代路线都不可用：

- `IApplicationViewCollection.GetViewForHwnd` + `MoveViewToDesktop`：`ImmersiveShell.QueryService` 对 `{1841C141-…}` 和 `CLSID_ImmersiveShell` 两种 service key 都返回 `E_NOTIMPL`；
- `Shift+Win+←/→` 系统加速键：对合成输入无反应（真实按键才行）；
- 各种指针封送写法（`byref` / `pointer` / `POINTER(GUID)(…)` / `cast`）：全是同一个 `E_ACCESSDENIED`，所以不是 comtypes 的封送问题。

工具因此选择“先真试、失败再翻译”：Win11 上同样一行代码能用，Win10 上给出指名道姓的拒绝而不是一个看着像权限 bug 的 Win32 码。

## 跑起来

包名 `dsh-gui-forkgo`，工具前缀 `mcp__gui__*`。

接法是 `--from windows-mcp` + `PYTHONPATH` 指向本目录，不是 `uvx --from <本目录>`。原因：
uvx 对本地目录的构建缓存不看文件 mtime，改完源码它照样复用上一次构建的产物——实测把整个
文案改成中文之后跑起来还是旧英文，而且没有任何提示。`PYTHONPATH` 让源码直接生效。

代价：本目录必须是**全量源码树**（完整的 `windows_mcp/` 包），不能是增量覆盖层，因为
PYTHONPATH 一旦命中，同名子模块全部从这里取。

`PYTHONPATH` 在 `dsh-bundle/cordis.patch.yml` 里由 `!!js` 从包位置反解，不写死绝对路径：
写死的话别人 clone 下来装，目录不存在，dsh 不报错而是静默落回 PyPI 上游。

改完在插件页把 `include:gui` 关掉再开即可（工具总数 22）。

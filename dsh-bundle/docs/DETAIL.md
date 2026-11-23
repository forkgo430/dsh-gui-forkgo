![Agent 截屏判断界面状态，再点击目标控件](preview.svg)

# GUI（桌面与窗口控制）

用 `@deepseek-ai/dsh-mcp-client` 起一个 stdio MCP 服务器 `windows-mcp`，把 Windows 的 UI Automation 和窗口管理包装成一组工具，模型就能像人一样看屏幕、点按钮、敲键盘、切换窗口、管理虚拟桌面。

## 提供什么

服务器名固定为 `gui`，工具名形如 `mcp__gui__<tool>`，共 **8 个**：

| 工具 | 干什么 | mode / action |
|---|---|---|
| `Screen` | 看屏幕 | `full`（控件树 + 图，默认）· `fast`（只出图）· `displays`（显示器与 DPI） |
| `Input` | 鼠标、键盘、剪贴板、等待 | `click` `type` `scroll` `move` `shortcut` `wait` `wait_for` `multi_select` `multi_edit` `clipboard_get` `clipboard_set` |
| `Programs` | 启动程序、管窗口、查杀进程 | `launch` `launch_executable` `resize` `switch` `list` `kill` |
| `System` | 命令、文件、注册表 | 文件 `read` `write` `copy` `move` `delete` `list` `search` `info`；注册表 `reg_get` `reg_set` `reg_delete` `reg_list`；命令 `shell` |
| `Scrape` | 抓网页（`use_dom=true` 读浏览器当前页） | — |
| `ControlStatus` | 谁在控制、空闲多少秒后交还、截图缓存与预热状态 | `release` |
| `VirtualDesktop` | 任务栏上那几组窗口 | `list` `current` `windows` `create` `switch` `rename` `remove` `move_window` |
| `IsolatedSession` | 独立子会话，不抢鼠标键盘；`start` 摆出实时预览窗 | `status` `setup` `start` `stop` `inject` `forget` |

### 从 15 个合并成 8 个

依据是参数包含关系，不是为了数字好看：

```
Snapshot + Screenshot + DisplayInventory  ->  Screen
App + Process                             ->  Programs
PowerShell + FileSystem + Registry        ->  System
Clipboard                                 ->  Input 的 clipboard_get / clipboard_set
```

`Screenshot` 的六个参数全在 `Snapshot` 里，`DisplayInventory` 一个参数都没有；
`Clipboard` 的 `text` 在 `Input` 里本来就是参数；`System` 三个工具的安全注解完全一致。

**合并的风险是同名参数语义不同**：`System` 的 `path` 在文件模式是文件路径、在
注册表模式是注册表键路径。所以每个 mode 都校验参数，传了不属于该 mode 的参数
直接报错并列出可用项，不静默忽略。`control_policy.py` 也必须同步 —— 没登记的工具
会被当成 `direct`，于是只读的 `Screen` 也去抢控制权，而这不会报错。

和内置 `pwsh` 工具的分工：`pwsh` 是命令行层（改文件、跑脚本、抓日志），GUI 是图形层（点安装向导、读弹窗、处理只有 GUI 才有的流程）。

## 运行方式

配置在 `cordis.patch.yml`，关键字段：

```yaml
serverName: gui
transport: stdio
command: <由 !!js 探测，依次试 %GUI_UVX% / ~/.local/bin / %UV_INSTALL_DIR% / LOCALAPPDATA / cargo / chocolatey / Program Files，落空退裸名 uvx>
args: ['--python', '3.14', '--from', 'windows-mcp==0.8.9', 'windows-mcp', 'serve', '--transport', 'stdio']
env:
  PYTHONUTF8: '1'
  PYTHONIOENCODING: 'utf-8'
  UV_NO_PROGRESS: '1'
  PYTHONPATH: '<本地 windows-mcp 源码树的绝对路径>'
toolCallTimeoutMs: 120000
startupTimeoutMs: 300000
```

`uvx` 每次启动自己维护一个隔离环境，首次运行要拉 Python 3.14 和依赖（pywin32、comtypes、dxcam 等），之后走缓存。`startupTimeoutMs` 给到 5 分钟就是为这一次冷启动留的余量；缓存热了之后启动在两三秒内。

`PYTHONPATH` 那行是可选的本地分支挂载点，原理和代价写在 `../README.md`。

## 虚拟桌面（`VirtualDesktop`）

上游把这个功能的 COM 封装写好了但没暴露成工具（只用来过滤窗口列表），这份分支把它挂了出来。

`action` 有 8 个：`list` / `current` / `windows` / `create` / `switch` / `rename` / `remove` / `move_window`。
`target` 接受 `desktop_id`、精确桌面名、从 1 开始的序号，或字面量 `current`。

本机（Windows 10 19045）实测出来的三条边界：

1. **没有桌面命名**。Win10 的 `IVirtualDesktopManagerInternal` vtable 里没有 `SetName`，`create(name=)` 和 `rename` 会在动手之前就拒绝，不会留下半个改动。
2. **新建不自动切换**。Win11 会切过去，Win10 不切；返回里的 `switched` 取自回读状态，不是假设。
3. **`move_window` 只能搬本进程的窗口**。`MoveWindowToDesktop` 对其它进程的窗口返回 `E_ACCESSDENIED`（不是权限问题：同一 token、同一会话，自己的窗口能搬），而本版本上 `IApplicationViewCollection` 不被 ImmersiveShell 提供、`Shift+Win+方向` 也忽略合成输入。所以调用会失败并说明原因，且不做任何改动。

**这不是输入隔离。** 切走以后鼠标键盘还是共享的，被切走的桌面里进程照跑、照出声。真要隔离得用 Terminal Server 子会话，那是另一个量级的东西。

## 装上之后注意

- 首次连接会弹一次 UAC 或防火墙提示是正常的，服务器要拿桌面句柄。
- 屏幕上有敏感窗口时别让它自己截屏——截图会作为图片进对话。
- `VirtualDesktop` 的 `create` / `switch` / `remove` / `move_window` 被归为“直接干预桌面”（`control_policy` 里是 `direct`），`relaxed` 策略下会先拿桌面所有权；`list` / `current` / `windows` / `rename` 是 `other`，不需要。
- 用不上了在**插件**页把 `include:gui` 关掉即可，进程随之退出，不会留后台常驻。

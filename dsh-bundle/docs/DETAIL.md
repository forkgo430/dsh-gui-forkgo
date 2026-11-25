![Agent 截屏判断界面状态，再点击目标控件](preview.svg)

# GUI（桌面与窗口控制）

用 `@deepseek-ai/dsh-mcp-client` 起一个 stdio MCP 服务器 `windows-mcp`，把 Windows 的 UI Automation 和窗口管理包装成一组工具，模型就能像人一样看屏幕、点按钮、敲键盘、切换窗口、管理虚拟桌面。

## 提供什么

服务器名固定为 `gui`，工具名形如 `mcp__gui__<tool>`，共 **8 个**（版本 **1.3.1**）：

| 工具 | 干什么 | mode / action |
|---|---|---|
| `Screen` | 看屏幕 | `full`（控件树 + 图，默认）· `fast`（只出图）· `displays`（显示器与 DPI）。另有 `force_image` 强制出图 |
| `Input` | 鼠标、键盘、剪贴板、等待 | `click` `type` `scroll` `move` `shortcut` `wait` `wait_for` `multi_select` `multi_edit` `clipboard_get` `clipboard_set` `sequence` |
| `Programs` | 启动程序、管窗口、查杀进程 | `launch` `launch_executable` `resize` `switch` `list` `kill` |
| `System` | 命令、文件、注册表 | 文件 `read` `write` `copy` `move` `delete` `list` `search` `info`；注册表 `reg_get` `reg_set` `reg_delete` `reg_list`；命令 `shell` |
| `Scrape` | 抓网页（`use_dom=true` 读浏览器当前页） | 无 mode，参数 `url` `query` `use_dom` `use_sampling` |
| `ControlStatus` | 谁在控制、空闲多少秒后交还、截图缓存与预热状态 | 参数 `release` |
| `VirtualDesktop` | 任务栏上那几组窗口 | `list` `current` `windows` `create` `switch` `rename` `remove` `move_window` |
| `IsolatedSession` | 独立子会话，不抢鼠标键盘；`start` 摆出实时预览窗 | `status` `setup` `start` `stop` `inject` `guard` `forget` |

`Input` 的 `sequence` 是一条调用跑多步：`steps` 传步骤数组（每步复用上面所有 action），
步间默认 120ms（`interval` 可覆盖），**硬上限 24 步**，序列里不能再套序列；
某步失败就停在那个失败的步，报告第几步失败、前面哪些已完成。

`IsolatedSession` 的 `guard` 管会话内守护：子会话里跑的代理，自动关掉 `#32770`
对话框（UAC、报错框、安装确认框）。`enable` 挂/撤，`close_dialogs` 控制挂上之后
关不关窗，`keep_titles` 用分号分隔标题白名单。

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
# command 必须是 !!js 探测，不能写死某台机器的绝对路径。
# 依次试 %GUI_UVX% → ~/.local/bin → %UV_INSTALL_DIR% → %LOCALAPPDATA%\Programs\uv
# → ~/.cargo/bin → chocolatey → C:\Program Files\uv，全落空才退回裸名 uvx 交给 PATH。
# 探测本身不抛错，所以配置求值一定成功 —— 求值失败会让整个插件行挂掉。
command: !!js >-
  (() => { ... 按上面顺序 fs.existsSync 逐个试，都落空就 return 'uvx' })()
# --from 钉了版本，别改回裸名。
args: ['--python','3.14','--from','windows-mcp==0.8.9','windows-mcp','serve','--transport','stdio']
env:
  PYTHONUTF8: '1'
  PYTHONIOENCODING: 'utf-8'
  UV_NO_PROGRESS: '1'
  # 同样由 !!js 从本包位置反解，不写死绝对路径
  PYTHONPATH: <本包位置>/vendor/windows-mcp
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

## 独立子会话（`IsolatedSession`）

`action` 有 7 个：`status` / `setup` / `start` / `stop` / `inject` / `guard` / `forget`。

AI 在另一个 Windows 会话里操作，用户的鼠标键盘完全不受影响。`start` 起一个**独立宿主进程**
建子会话（默认 1280x720），等 `explorer.exe` 出现才算就绪，约 16 秒；插件重载不影响它，
但宿主一退会话就掉线。

`start` 会在主屏右下角摆一个**预览窗**，里面是子会话的实时画面，置顶、可拖动、有关闭按钮。
`status` 的 `session_detail.preview_window` 会报 `visible` / `minimized` / `offscreen` / `rect`
和结论字段 `on_screen`。`Screen` 截的永远是**用户自己的桌面**（MCP 进程在 Session 1），
子会话画面只存在于那个 RDP 控件内部，所以别把 `visible` 关掉。

### 会话内守护（`guard`）

子会话里跑一个守护进程，负责关掉 `#32770` 对话框（UAC、报错框、安装确认框）。
**父会话关不掉这些窗**：Windows 按 session 隔离窗口站，实测父会话枚举到的子会话顶层
窗口数是 0（自己的能看到 300 多个）。

投放方式是在建会话之前把守护脚本挂到 **HKCU 的 Run 键**，子会话登录时系统在子会话里
执行它 —— HKCU 是各会话共享的同一份注册表。`start` 默认自动挂载，`stop` 自动撤下。
`status` 的 `guard.running` 由心跳判定（超 8 秒没刷新算停），`guard.closed_total` 是关了几个。

### 这个版本没做成的

诚实地记在这里，免得下次又去试：

| 想做的 | 结果 |
|---|---|
| `ConnectToShadowSession` 接 shadow 会话 | 序列完整实现了（`WTSEnableChildSessions` → `Disconnect` 预热 → `AllowPromptingForCredentials(false)` → `put ConnectToShadowSession` → 回读确认），但本机 `mstscax.dll`（10.0.19041.4522）里 ASCII/UTF-16 双格式搜索 `ConnectToShadowSession` 各 0 次命中，只有 `ConnectToChildSession`。拿不到，**自动回落 child** |
| `IMsRdpInputSink` | 没做。vtable 顺序无公开头文件可确认，本机 mstscax 版本已证明落后 |
| 注入式代理 DLL（豆包 `pip_remote.dll` 那条路） | 没做，**这台机器没有编译器**。等价替代是上面的 HKCU Run 键 + 守护脚本 |

## 装上之后注意

- 首次连接会弹一次 UAC 或防火墙提示是正常的，服务器要拿桌面句柄。
- 屏幕上有敏感窗口时别让它自己截屏——截图会作为图片进对话。
- `VirtualDesktop` 的 `create` / `switch` / `remove` / `move_window` 被归为“直接干预桌面”（`control_policy` 里是 `direct`），`relaxed` 策略下会先拿桌面所有权；`list` / `current` / `windows` / `rename` 是 `other`，不需要。
- `Input` 的每个 action 都是 `direct`（含 `clipboard_get`，它读的是全局共享状态）；`Screen` / `Scrape` / `IsolatedSession` 是 `other`，不抢控制权。
- **改了 `vendor/windows-mcp` 里的 Python 源码，必须删掉 `__pycache__` 再重启**，否则 `.pyc` 的旧字节码会盖住新代码，且没有任何征兆。
- 用不上了在**插件**页把 `include:gui` 关掉即可，进程随之退出，不会留后台常驻。

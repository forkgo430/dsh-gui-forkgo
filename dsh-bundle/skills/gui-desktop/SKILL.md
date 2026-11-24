---
name: gui-desktop
description: 通过 GUI（windows-mcp）MCP 操作整台 Windows 机器：看屏幕、点击输入、快捷键拖拽、剪贴板、窗口与进程、文件、注册表、命令、虚拟桌面、控制权。当用户要求"帮我在电脑上点/输入/打开某个程序/看屏幕上有什么/操作只有界面才能完成的流程"时使用。强制走「先观察后动手 → 精确定位 → 小步验证 → 控制权交还」四步，避免盲点、点错位置、把用户的鼠标抢走不放。仅限 Windows；纯命令行能解决的任务不要用本技能。
license: MIT
compatibility: Windows 10/11；需要 windows-mcp 服务器进程在运行（GUI 插件挂载）
metadata:
  author: 赢容
  version: "2.0.0"
---

# 在 Windows 上操作界面的正确链路

> 作者：赢容（与「WPS 多面手」同一位作者）

工具不缺（8 个，`mcp__gui__*`），缺的是**按什么顺序用**。
本技能只管顺序和判断，不重复工具清单 —— 清单会过期，以 `tools/list` 为准。

## 八个工具，先认清楚

| 工具 | 一句话 | 找它的哪个参数 |
|---|---|---|
| `Screen` | 看屏幕 | `mode`：`full` 控件树 + 图 / `fast` 只出图 / `displays` 显示器与 DPI |
| `Input` | 动手 | `action`：click / type / scroll / move / shortcut / wait / wait_for / multi_select / multi_edit / clipboard_get / clipboard_set |
| `Programs` | 开程序、管窗口、查杀进程 | `mode`：launch / launch_executable / resize / switch / list / kill |
| `System` | 命令、文件、注册表 | `mode`：文件 8 种 + 注册表 `reg_*` 4 种 + `shell` |
| `Scrape` | 抓网页 | `url` |
| `ControlStatus` | 谁在控制 | `release=true` 交还 |
| `VirtualDesktop` | 任务栏那几组窗口 | `action` 八种 |
| `IsolatedSession` | 独立子会话（不抢键鼠） | `action`：status/setup/start/stop/inject/forget |

**参数认 mode，不认工具。** 从 15 个合并成 8 个之后，`mode` 决定参数的含义：
`System(mode="read", path="C:\\a.txt")` 读文件，`System(mode="reg_get", path="HKCU:\\X")`
读注册表。传错 mode 会报错并列出该 mode 可用的参数，不会静默写错地方。

## 什么时候用 / 不用

**用**：需要看屏幕、点按钮、填表单、开程序、读弹窗、操作只有 GUI 才有的流程
（安装向导、登录框、没有 CLI 的老软件、要拖拽的界面）。

**不用**：
- 改文件、跑脚本、抓日志 → 用 `pwsh` / `read` / `write`，比点界面快一百倍
- 纯知识问答、代码任务
- 已经在对话里的内容（不需要去屏幕上找）

**先问自己**：这件事有命令行做法吗？有就别开 GUI。

## 四步主链路

### 1. 先观察后动手

动手之前一定先看。三种看法，按需选：

| 用法 | 什么时候用 | 代价 |
|---|---|---|
| `Screen(mode="full")` | **优先**。标准控件都能报出角色、名称、坐标 | 纯文本，便宜 |
| `Screen(mode="full", use_vision=True)` | 要在画面上判断位置、看排版、认图标 | 附图，贵 |
| `Screen(mode="fast")` | 只要一眼画面，不要控件树 | 总是附图 |
| `Screen(mode="displays")` | 查显示器布局、DPI、缩放，不截图 | 极便宜 |

**默认走 `full` 不附图**。Electron / 浏览器 / 原生 Win32 的按钮都在控件树里，
直接给你可点的坐标，不用从图上量。只有 canvas、WebGL、游戏、图片里的文字
才必须看图。

`Screen` 支持 `region=[left,top,right,bottom]`，**一定要用**：全屏快照会把任务栏、
壁纸、无关窗口全塞进来，既贵又乱。锁到你要操作的那一块。

**帧没变就别再量坐标。** 返回里的 `unchanged=true` 说明画面和上一帧一模一样，
你手上那张图和它上面的坐标仍然有效，直接用，不用重新 `Screen`。
`force_image=true` 可以强制出图，但只影响当次，不会关掉去重。

> 注意：全屏连拍通常永远是 `unchanged=false`（桌面总有像素在变），
> **静止区域**才会触发去重。所以 `region` 锁得越准，越容易拿到这个信号。

返回的 `Coordinate Contract` 行会告诉你这张图和屏幕的换算关系。**只认这行**：
1:1 时直接用，缩放时按它给的倍率乘，控件树里的坐标是屏幕物理像素、**不乘**。

### 2. 精确定位

拿到坐标后有两种点法：

- **按坐标点**：`Input(action="click", loc=[x,y])` —— 坐标是屏幕像素，和 `Screen` 报的一致，直接用。
- **按控件点**：`Input(action="click", label=<控件编号>)` —— 比坐标稳，控件移动了也不会点空。

**坐标不要猜**。看着像 (300,400) 就点 (300,400) 是本技能最常见的失败原因。
每次点之前问自己：这个坐标是刚才哪一次观察报出来的？

**一次调多步用 `sequence`：** `Input(action="sequence", steps=[{...}, {...}])`，
最多 24 步，步间默认 120ms（`interval` 可配）。适合「点 A → 填 B → 点确定」这种
紧凑连动，省往返。

**某一步失败就停下**，会告诉你第几步失败、已完成哪些，不会默默继续。
`wait` / `wait_for` 这类等步不要用 `sequence`，等的时间不确定，会把后面的步挤在一起。

`Input(action="move")` 只移动不点击，用来触发悬停菜单；`scroll` 在指定点滚动，
`wheel_times` 控制量。`multi_edit` 一次填多个输入框，`multi_select` 处理多选
（`press_ctrl` 控制是否按住 Ctrl）。

### 3. 小步验证

**一次动作，一次确认。** 不要连续点五次然后才看结果。

- 点完 → `Screen(mode="full", region=同一块)` 看有没有出现预期变化
- 输入完 → 读回来看值对不对（`type` 的 `clear` / `caret_position` 会影响结果）
- 打开程序 → `Input(action="wait_for", condition="text_exists", text=...)` 比固定等待可靠

连续动作要合并时用 `multi_edit` / 多步脚本，但**每步之间要留时间**，
给界面反应的机会。太快会点到还没画出来的东西上。

### 4. 控制权交还

这个工具会**抢占真实鼠标键盘**。人在键盘前动一下就会打架。

- 开场先 `ControlStatus` 看当前状态（`state` / `policy` / `idle_timeout_seconds`）
- 干完活主动 `ControlStatus(release=true)` 交还，**不要让用户等你**
- 需要长时间操作时，先说一句"接下来会接管鼠标键盘"

默认策略是 `relaxed`：`Input` 的所有动作会先拿桌面所有权（含剪贴板）；
`Programs` 的 `resize` / `switch` 也拿，`launch` / `list` / `kill` 不拿；
`Screen` / `Scrape` / `IsolatedSession` 完全不拿，可以随便调。

## 虚拟桌面（`VirtualDesktop`）

`action` 八个：`list` / `current` / `windows` / `create` / `switch` / `rename` /
`remove` / `move_window`。
`target` 接受 `desktop_id`、精确名称、从 1 开始的序号、或 `current`。

**本机是 Windows 10 19045，有三条硬限制**（换机器要重新 `list` 看 `naming` 字段）：

| 限制 | 表现 | 该怎么办 |
|---|---|---|
| 不支持命名 | `rename` / `create(name=)` 抛 `NotImplementedError`，`name` 恒为 null | 用 `desktop_id` 或序号指定 |
| 新建不自动切换 | `create` 返回 `switched:false` | 要过去就再调 `switch` |
| `move_window` 只搬本进程窗口 | 别的进程的窗口抛 `PermissionError` | 不动它，直接在那个桌面上操作 |

**这不是输入隔离。** 切走以后鼠标键盘还是共享的，被切走的桌面里进程照跑、照出声。
要真正隔离得用 `IsolatedSession`，那是另一个量级的东西。

## 隔离会话（`IsolatedSession`）

要长时间操作、又不想抢用户键鼠时用它。`action` 六个：

| action | 干什么 | 代价 |
|---|---|---|
| `status` | 查能不能建、凭据存了没、宿主在不在跑、预览窗在不在屏幕上 | 无 |
| `setup` | 弹一次 Windows 凭据框，密码用 DPAPI 加密存本机 | 要输一次密码 |
| `start` | 起一个常驻宿主进程建子会话，默认 1280×720 | 约 16 秒 |
| `stop` | 让宿主断开，`WTSLogoffSession` 注销会话 | 约 2 秒 |
| `inject` | 往子会话的 explorer 里注入 DLL | 需要 `dll_path` |
| `forget` | 删掉存下的凭据 | — |

**`start` 会在主屏右下角摆一个预览窗**，里面是子会话的实时画面。用户能看见
AI 在里面做什么，但 AI 的动作发生在另一个会话里，碰不到用户的键鼠。

三个参数控制它：`visible`（默认 true，传 false 回到完全不可见）、
`left` / `top`（指定位置，例如挪到副屏）。

**`Screen` 永远看不到子会话。** 它截的是用户自己的桌面（MCP 进程在 Session 1
里跑），子会话画面只存在于那个 RDP 控件内部。想知道子会话里发生了什么，
唯一的途径是看预览窗——所以别把 `visible` 关掉，除非用户明确要求。

`status` 的 `session_detail.preview_window.on_screen` 告诉你窗口到底在不在屏幕上。

**子会话的存活绑在宿主进程上。** 宿主一退会话立刻掉线。插件重载不影响它 ——
宿主是独立进程。用完记得 `stop`，不然会堆孤儿会话，每个几百 MB。
用户手动关掉预览窗也等于 `stop`（会断连并写停止标记）。

## 三条红线

### 一、`Screen` 之后再点，不要反过来

坐标只有一个来源：刚才那次观察。改窗口大小、切标签、弹新窗口都会让旧坐标失效。
**如果两次动作之间过了一秒以上，或者有任何界面变化，重新 `Screen`。**

### 二、破坏性操作先想清楚在改什么

`System` 的 `shell` / `write` / `delete` / `reg_set` / `reg_delete`、`Programs` 的
`kill`、`Input` 的 `clipboard_set` 都能改东西。

- 删文件、改注册表、杀进程、关闭文档之前**先说一句**，别默默干
- `System(mode="shell")` 是整台机器的 shell，不是沙箱 —— 它不受 DSH 的文件沙箱限制
- 写文件优先用 `write` / `edit` 工具，不必绕道 `shell`
- `clipboard_set` 覆盖的是用户真的剪贴板，别拿它当工具之间的传参通道

### 三、窗口标题别当唯一标识

按 `title` 找窗口时，标题可能重复（两个记事本、多个浏览器标签）。
`VirtualDesktop` 的 `move_window` 遇到多个匹配会**直接报错并列出候选**，不会瞎选。
`Screen(mode="full")` 的窗口列表带 `handle`，用 handle 更稳。

## 常见任务的正确起手

| 任务 | 起手 |
|---|---|
| "帮我看看屏幕上有什么" | `Screen(mode="full", region=关心的区域)` 不附图 |
| "点一下那个按钮" | `Screen(mode="full", region=那块)` → 从控件树拿坐标 → `Input(action="click", loc=...)` |
| "在那个输入框里填内容" | `Screen` 确认输入框坐标 → `click` 聚焦 → `type` |
| "打开某某程序" | 先 `pwsh` 试 `Start-Process`；GUI 流程才用 `Programs(mode="launch")` |
| "等某个东西出现" | `Input(action="wait_for", condition="text_exists")`，别用固定等待 |
| "连点一串按钮/填一串表单" | `Input(action="sequence", steps=[...])`，一次调用跑完，比一步步省往返 |
| "帮我整理窗口" | `VirtualDesktop` 建桌面 → `move_window`（只对本进程窗口有效） |
| "这台机器几个屏、缩放多少" | `Screen(mode="displays")` |
| "别抢我鼠标" | `IsolatedSession(action="start")` |

## 参考

- 工具清单与参数：`tools/list`（8 个，别凭记忆写参数名）
- 屏幕提示文案（蓝光/顶部面板/接管确认框）改在哪：`vendor/windows-mcp/windows_mcp/desktop/control_overlay_art.py`
  与 `control_consent.py`，都在本 bundle 内
- 哪个工具会抢控制权：`vendor/windows-mcp/windows_mcp/tools/control_policy.py`

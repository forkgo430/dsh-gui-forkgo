---
name: gui-desktop
description: 通过 GUI（windows-mcp）MCP 操作整台 Windows 机器：截屏观察、点击、输入、快捷键、拖拽、窗口与进程、文件、注册表、PowerShell、剪贴板、虚拟桌面、控制权。当用户要求"帮我在电脑上点/输入/打开某个程序/看屏幕上有什么/操作只有界面才能完成的流程"时使用。强制走「先观察后动手 → 精确定位 → 小步验证 → 控制权交还」四步，避免盲点、点错位置、把用户的鼠标抢走不放。仅限 Windows；纯命令行能解决的任务不要用本技能。
license: MIT
compatibility: Windows 10/11；需要 windows-mcp 服务器进程在运行（GUI 插件挂载）
metadata:
  author: 赢容
  version: "1.0.0"
---

# 在 Windows 上操作界面的正确链路

> 作者：赢容（与「WPS 多面手」同一位作者）

工具不缺（15 个，`mcp__gui__*`），缺的是**按什么顺序用**。
本技能只管顺序和判断，不重复工具清单 —— 清单会过期，以 `tools/list` 为准。

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

动手之前一定先看。两种看法，按需选：

| 方式 | 什么时候用 | 代价 |
|---|---|---|
| `Snapshot`（默认 `use_vision=False`） | **优先**。标准控件都能报出角色、名称、坐标 | 纯文本，便宜 |
| `Snapshot(use_vision=True)` | 要在画面上判断位置、看排版、认图标 | 附图，贵 |
| `Screenshot` | 只要一眼画面，不要控件树 | 总是附图 |

**默认走 Snapshot 不附图**。Electron / 浏览器 / 原生 Win32 的按钮都在控件树里，
直接给你可点的坐标，不用从图上量。只有 canvas、WebGL、游戏、图片里的文字
才必须看图。

`Snapshot` 支持 `region=[left,top,right,bottom]`，**一定要用**：全屏快照会把任务栏、
壁纸、无关窗口全塞进来，既贵又乱。锁到你要操作的那一块。

### 2. 精确定位

拿到坐标后有两种点法：

- **按坐标点**：`Click(loc=[x,y])` —— 坐标是屏幕物理像素，和 `Snapshot` 报的一致，直接用。
- **按控件点**：先用 `Snapshot(includeValues=...)` 拿到控件，再按名称/索引操作。

**坐标不要猜**。看着像 (300,400) 就点 (300,400) 是本技能最常见的失败原因。
每次点之前问自己：这个坐标是刚才哪一次观察报出来的？

`Move` 只移动不点击，用来触发悬停菜单；`Scroll` 在指定点滚动，`wheel_times` 控制量。
`MultiEdit` 可以一次填多个输入框，`MultiSelect` 处理多选（`press_ctrl` 控制是否按住 Ctrl）。

### 3. 小步验证

**一次动作，一次确认。** 不要连续点五次然后才看结果。

- 点完 → `Snapshot(region=同一块)` 看有没有出现预期变化
- 输入完 → 读回来看值对不对（`Type` 的 `clear` / `caret_position` 会影响结果）
- 打开程序 → `WaitFor(text_exists=...)` 等比 `Wait(固定秒数)` 可靠

连续动作要合并时用 `MultiEdit` / 多步脚本，但**每步之间要留 `settleMs`**，
给界面反应的时间。太快会点到还没画出来的东西上。

### 4. 控制权交还

这个工具会**抢占真实鼠标键盘**。人在键盘前动一下就会打架。

- 开场先 `ControlStatus` 看当前状态（`state` / `policy` / `idle_timeout_seconds`）
- 干完活主动 `ControlStatus(release=True)` 交还，**不要让用户等你**
- 需要长时间操作时，先说一句"接下来会接管鼠标键盘"

默认策略是 `relaxed`：`Click` / `Type` / `Scroll` / `Move` / `Shortcut` /
`MultiEdit` / `MultiSelect` 会先拿桌面所有权；`Snapshot` / `Screenshot` /
`Wait` / `WaitFor` / `DisplayInventory` / `Scrape` 不拿，可以随便调。

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
要真正隔离得用 Terminal Server 子会话，那是另一个量级的东西。

## 三条红线

### 一、`Snapshot` 之后再 `Click`，不要反过来

坐标只有一个来源：刚才那次观察。改窗口大小、切标签、弹新窗口都会让旧坐标失效。
**如果两次动作之间过了一秒以上，或者有任何界面变化，重新 Snapshot。**

### 二、破坏性操作先看 annotations

`PowerShell` / `FileSystem` / `Registry` / `Process(kill)` / `App` / `Click` /
`Type` / `MultiEdit` / `Shortcut` 这些能改东西。

- 删文件、改注册表、杀进程、关闭文档之前**先说一句**，别默默干
- `PowerShell` 是整台机器的 shell，不是沙箱 —— 它不受 DSH 的文件沙箱限制
- 写文件优先用 `write` / `edit` 工具，不要绕道 PowerShell

### 三、窗口标题别当唯一标识

按 `title` 找窗口时，标题可能重复（两个记事本、多个浏览器标签）。
`VirtualDesktop.move_window` 遇到多个匹配会**直接报错并列出候选**，不会瞎选。
`Snapshot` 的窗口列表带 `handle`，用 handle 更稳。

## 常见任务的正确起手

| 任务 | 起手 |
|---|---|
| "帮我看看屏幕上有什么" | `Snapshot(region=关心的区域)` 不附图 |
| "点一下那个按钮" | `Snapshot(region=那块)` → 从控件树拿坐标 → `Click(loc=...)` |
| "在那个输入框里填内容" | `Snapshot` 确认输入框坐标 → `Click` 聚焦 → `Type` |
| "打开某某程序" | 先 `pwsh` 试 `Start-Process`；GUI 流程才用 `App(launch=...)` |
| "等某个东西出现" | `WaitFor(text_exists=...)`，不要 `Wait(10)` |
| "帮我整理窗口" | `VirtualDesktop` 建桌面 → `move_window`（只对本进程窗口有效） |

## 参考

- 工具清单与参数：`tools/list`（15 个，别凭记忆写参数名）
- 屏幕提示文案（蓝光/顶部面板/接管确认框）改在哪：`vendor/windows-mcp/windows_mcp/desktop/control_overlay_art.py`
  与 `control_consent.py`，都在本 bundle 内

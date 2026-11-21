# GUI · DSH 的图形层

**简体中文**

[![CI](https://github.com/forkgo430/dsh-gui-forkgo/actions/workflows/ci.yml/badge.svg)](https://github.com/forkgo430/dsh-gui-forkgo/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![tools](https://img.shields.io/badge/tools-22-brightgreen.svg)](#提供什么)
[![platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey.svg)](#环境要求)

让 AI 操作 Windows 界面：截屏观察、点击、输入、快捷键、窗口与进程、虚拟桌面。

改文件、跑脚本这类事用 `pwsh` 更快。GUI 针对的是命令行碰不到的流程：安装向导、
登录弹窗、没有 CLI 的老软件、要拖拽才能完成的界面。

<sub>作者：**赢容** · 与 [WPS 多面手](https://github.com/forkgo430/wps-multitool-mcp) 同一作者</sub>

## 30 秒上手

把 `dsh-bundle/` 装进 DSH：

```powershell
# 插件页 → 添加插件 → 本地目录 → 选本仓的 dsh-bundle/
# 或
plugin_manager install_bundle，target 指到 dsh-bundle/ 的绝对路径
```

重启插件行后可以直接说：

> 帮我看看屏幕上现在是什么

> 点一下那个"下一步"

> 把记事本里那段文字复制出来

其他例子：

- "等安装向导出现，一路点下一步"
- "在浏览器的地址栏输入 xxx 然后回车"
- "把这两个窗口分到两个虚拟桌面上"

## 提供什么

服务器名是 `gui`，工具名形如 `mcp__gui__<工具名>`，共 15 个：

| 能力 | 工具 | 说明 |
|---|---|---|
| 观察 | `Snapshot` · `Screenshot` · `DisplayInventory` | 控件树带可点坐标；`use_vision=False` 时只返回文本，不附图 |
| 输入 | `Input` | 一个入口。`action` 分发：click / type / scroll / move / shortcut / wait / wait_for / multi_select / multi_edit |
| 鼠标 | `Click` · `Move` · `Scroll` · `MultiSelect` | 按坐标或按控件；拖拽、滚轮、多选 |
| 键盘 | `Type` · `Shortcut` · `MultiEdit` | 输入、快捷键、批量填多格 |
| 窗口与程序 | `App` · `Process` | 启动、切换、调整窗口、查进程 |
| 文件与系统 | `FileSystem` · `Registry` · `PowerShell` · `Clipboard` · `Scrape` · `Notification` | |
| 控制权 | `ControlStatus` | 谁在控制、空闲多久交还 |
| **虚拟桌面** | **`VirtualDesktop`** | 任务栏那几组窗口：列出、新建、切换、重命名、删除、搬窗口 |
| **隔离会话** | **`IsolatedSession`** | 独立子会话：AI 在另一个桌面里操作，不抢你的鼠标键盘。`status` 查能力、`setup` 存密码一次、`start`/`stop` 起停常驻宿主、`inject` 注入 DLL、`forget` 删凭据 |

另有一份技能 `gui-desktop`，说明这些工具的使用顺序。

### 三处实现上的取舍

**`Snapshot` 默认不附图。** 只返回控件树，含角色、名称、坐标、可用动作、快捷键。
坐标可以直接用来点击。canvas、WebGL 和图片里的文字不在控件树里，这些情况才需要开 `use_vision`。

**`VirtualDesktop` 是本仓加的。** 上游 `windows-mcp` 写好了虚拟桌面的 COM 封装，
只用来过滤窗口列表，21 个工具里没有一个暴露它。这里挂成工具，并改掉上游封装
三处会让工具报假成功的地方。

**屏幕提示是中文的。** 接管桌面时四周有蓝光、顶部中间一块提示，接管前还有确认框：

```
┌─────────────────────────────────────┐
│        AI 正在控制这台电脑           │
│          大幅移动鼠标                │
│   或按 Ctrl+Alt+Shift+Backspace 接管 │
└─────────────────────────────────────┘
```

接管方式有两种：大幅移动鼠标，或按 `Ctrl + Alt + Shift + Bac

**`IsolatedSession` 也是本仓加的（1.2.0）。**

它解决的是「AI 抢鼠标键盘」这个具体问题。做法是在同一台机器上开一个**子会话**
（Windows 的 Child Session），AI 在那个桌面里操作，你的桌面完全不受影响。

要一次 Windows 密码：首次 `setup` 会弹系统标准凭据框，密码用当前账户的 DPAPI
加密后存进凭据管理器 `dsh-gui/Session`，之后不再问。存的是**标准 DPAPI blob**，
不是自定义方案。

三点已知约束，先说在前面：

- **子会话的存活绑在 RDP 宿主进程上**，宿主一退会话就掉线。`start` 动作会起一个
  独立的宿主进程（窗口挪到屏幕外，用户看不见），插件重载不影响它。
- 一个系统同一时刻只能有一个活动子会话。
- **能用 UIA 控件模式做的事本来就不动真实指针**，那些场景不需要子会话。
  子会话只解决 canvas、自绘界面这类 UIA 覆盖不到的情况。

用法四步：

```
IsolatedSession(action="setup")   → 弹一次凭据框，存下来
IsolatedSession(action="start")   → 起宿主建会话，约 5 秒就绪
IsolatedSession(action="status")  → 确认 state=active、explorer 在
IsolatedSession(action="stop")    → 收工，子会话销毁
```

调查与实测的完整记录（含微软文档没写的那条例外、注入链路验证、账户锁定的教训）
在 [`docs/虚拟桌面计划.md`](docs/虚拟桌面计划.md)。
kspace`。
面板在 1920 宽的屏上约 398×114，占屏幕 21%×11%；窄屏自动缩到 276×97。

## 环境要求

| 要求 | 说明 |
|------|------|
| Windows 10 / 11 | UI Automation 与虚拟桌面 API 只在 Windows 可用 |
| DSH | 需要插件系统与 MCP 客户端（`@deepseek-ai/dsh-mcp-client`） |
| [`uvx`](https://docs.astral.sh/uv/) 在 PATH 上 | 首次启动会拉 Python 3.14 与 pywin32、comtypes、dxcam |

## 它怎么工作

```mermaid
flowchart LR
    A["DSH<br/>loader"] -->|按包名解析| B["dsh-bundle<br/>只有配置与资产"]
    B -->|spawn| C["uvx"]
    C -->|拉依赖 + PYTHONPATH 覆盖源码| D["windows-mcp<br/>stdio MCP server"]
    D -->|22 个工具| A
    B -->|customSkillDirs| E["gui-desktop<br/>SKILL.md"]
    E --> A
```

bundle 里没有可执行代码，只有配置和资产。实际执行的是 `uvx` 拉起的 `windows-mcp` 子进程。

## 边界

**虚拟桌面不隔离输入。** 切换之后鼠标键盘仍然共享，被切走的桌面里进程照常运行、
照常出声。要真正隔离得用 Terminal Server 子会话，工作量和这个不是一个量级。

**Windows 10 19045 上实测到三条限制**，工具会直接拒绝，不返回假成功：

| 限制 | 表现 | 原因 |
|---|---|---|
| 不支持桌面命名 | `rename` / `create(name=)` 抛 `NotImplementedError` | 命名是 Win11 22000+ 的能力，19045 的 vtable 里没有 `SetName` |
| 新建不自动切换 | `create` 返回 `switched:false` | Win11 会切，Win10 不切 |
| `move_window` 只搬本进程窗口 | 别的进程的窗口抛 `PermissionError` | 见下 |

第三条试过三条替代路线，在这个版本上都不可用：
`IApplicationViewCollection.GetViewForHwnd` 加 `MoveViewToDesktop`（`QueryService` 返回 `E_NOTIMPL`）、
`Shift+Win+←/→` 系统加速键（忽略合成输入）、各种指针封送写法（都是同一个 `E_ACCESSDENIED`）。
和权限无关：同一 token、同一会话下，自己的窗口能搬，别人的不能。

## 结构

```
dsh-gui-forkgo/
├── dsh-bundle/              ← 装进 DSH 的就是这个目录
│   ├── package.json
│   ├── cordis.patch.yml     挂 MCP 客户端 + 技能扫描根
│   ├── skills/
│   │   └── gui-desktop/SKILL.md
│   ├── vendor/windows-mcp/  windows-mcp 0.8.9 源码树（本分支改动在这里）
│   ├── locale/{zh,en}.json
│   ├── docs/DETAIL.md
│   └── selftest.js          结构 + 技能 + 连接层自检
├── .github/workflows/ci.yml
├── package.json
├── CHANGELOG.md
├── README.md
└── LICENSE
```

## 自检

改完先跑一遍，不需要 dsh：

```powershell
npm run selftest           # 结构 + 技能 + 连接层
npm run selftest:offline   # 只验结构与技能
```

连接层会真起一次 MCP。依赖已经在缓存里时约 5 秒，首次要拉 Python 3.14 和依赖，
实测 11 秒左右。

它挡的是三类「改对了也看不出来」的问题：

1. **包结构** —— `dsh.bundle.patch` 是否存在、`args` 里有没有相对路径、
   `PYTHONPATH` 是不是由 `!!js` 反解而不是写死的绝对路径、`vendor/` 是不是完整的源码树。
2. **技能 frontmatter** —— `name` 是否为 kebab-case 且与目录名一致、`description` 是否为空。
   缺 description 时 dsh 不会加载，也不报错，表现是「AI 不会用」。
3. **连接层** —— 真起 stdio、列工具，确认 15 个工具里 `VirtualDesktop` 在、屏幕文案是中文。

## 与上游的关系

包本体是 [`windows-mcp`](https://pypi.org/project/windows-mcp/) 0.8.9 的副本加改动，
放在 `dsh-bundle/vendor/windows-mcp/`，用 `PYTHONPATH` 覆盖；依赖仍由 `uvx` 从 PyPI 安装。
上游是 MIT，版权声明随附在 `vendor/windows-mcp/LICENSE-upstream.md`。

不用 `uvx --from <本地目录>` 的原因：uvx 对本地目录的构建缓存不看文件 mtime，
改完源码它照样复用上一次构建的产物。实测把整份文案改成中文之后跑起来还是英文，
也没有任何提示。`PYTHONPATH` 能让源码直接生效。

代价是那个目录必须放完整源码树，不能只放改动的文件。`PYTHONPATH` 一旦命中，
同名子模块全部从那里取，缺文件就是 ImportError。

## 隐私

- 屏幕内容不出本机：没有遥测、没有崩溃上报、没有使用统计
- 唯一的出站行为是 `uvx` 首次启动时从 PyPI 拉依赖
- 截图落在 DSH 会话目录下（`.dsh-pilot/` 或你指定的 `save_path`）。敏感窗口不要截

## 已知限制

- 只支持 Windows：UI Automation 和虚拟桌面 API 都是 Windows 专有
- 会占用真实的鼠标键盘。这不是输入隔离，人在键盘前动一下就会互相干扰
- `PowerShell` 是整机 shell，不受 DSH 文件沙箱限制。删文件、改注册表之前要先说一声
- canvas、WebGL、游戏画面这类只有像素没有控件树的界面必须走 `use_vision`，
  成本更高，而且要靠看图判断

## 故障排除

| 问题 | 解决方案 |
|------|---------|
| 工具列表里没有 `mcp__gui__*` | 确认 `include:gui` 是 active；看插件页有没有启动失败 |
| `VirtualDesktop` 不存在 | `vendor/windows-mcp` 没被带上，跑的是 PyPI 上游的 21 工具版 |
| 屏幕提示还是英文 | 同上，`PYTHONPATH` 没指到 `vendor/` 里的源码 |
| 技能不出现 | 跑 `npm run selftest`；缺 description 会跳过且不报错 |
| 首次调用很慢 | `uvx` 冷启动要拉 Python 3.14 与依赖，约 5 秒；之后走缓存 |
| `initialize` 超时 | 先手动跑一次 `uvx --python 3.14 --from windows-mcp windows-mcp --help` 预热 |

## 许可

MIT。见 [LICENSE](LICENSE)。

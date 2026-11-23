# Changelog

本项目的版本沿革。包名轨迹：`@local/windows-mcp` → `@local/gui` → **`dsh-gui-forkgo`**。

## 未发布

两处「只在开发机上成立」的配置，换台机器就复现不出来，别人装上直接起不来。

### 修复

**`command` 写死了开发机的 uvx 绝对路径。**（严重：别人的机器上必挂）

`cordis.patch.yml` 里原本是 `'C:\Users\金东权\.local\bin\uvx.exe'`。任何别的机器上
这个文件都不存在，spawn 直接 ENOENT，而 dsh 把它压成一句
`mcp-client(gui): initial connection or tool synchronization failed` —— 看不出是缺 uv。

改成 `!!js` 依次探测：`%GUI_UVX%` → `~/.local/bin` → `%UV_INSTALL_DIR%` →
`%LOCALAPPDATA%\Programs\uv` → `~/.cargo/bin` → chocolatey → `C:\Program Files\uv`，
全落空才退回裸名 `uvx` 交给 PATH。探测本身不抛错，所以配置求值一定成功——
求值失败会让整个插件行挂掉，连「uvx 没装」这条线索都拿不到。

**`--from windows-mcp` 没钉版本。**（中：干净机器行为不可复现）

开发机上 uv 早缓存了 0.8.9 的解析结果，所以一直没事。干净机器会现拉最新版
（现为 0.9.0），上游每个小版本都可能动 CLI 参数和依赖图。已钉成
`windows-mcp==0.8.9`，与 `vendor` 里的源码树对齐。**升级时两处一起改。**

### 顺带

**删掉面板外面那层光晕。**

顶部提示面板外面原本还套了一圈 `_notice_glow_bitmap`，与四周的框光是同一套蓝色，
屏幕上同时出现两层蓝。实测面板区蓝通道均值：两层 74.9 → 只有面板 49.3，
光晕自己贡献了约 22，肉眼能单独看见。框光承担「AI 在控」的主信号，面板只负责
说按什么键接管，再套一圈同类光晕属于重复表达。

`_notice_glow_bitmap` 本身留着（预览图和几何测试还用），只是实时指示器不再调它。

**自检加了两条检查**：`command` 不许是绝对路径、`--from` 必须钉版本。
这两类问题都是「改对了也看不出来」，不卡在脚本里就会漏出去。

## 1.2.2

修四个实测发现的 bug。前两个是严重的资源问题。

> **注意：npm 上的 1.2.2 只有前三条修复。**
> 第四条（智能体空闲自动交还）是 npm 审核期间补的，当时 1.2.2 已经在暂存队列里
> 不能覆盖，所以只进了本地和 GitHub，没进 npm。版本号相同但内容不同。
> 要完整功能请从本地目录或 GitHub 装。

### 修复

**智能体忘了 `release()`，叠加层会一直挂在屏幕上。**（仅本地/GitHub）

实测踩到：智能体跑完一轮对话没交还控制权，四周蓝光和顶部提示面板在屏幕上挂了
七十多分钟，用户以为程序卡死。

`idle_timeout_seconds`（默认 10 秒）管的是**反方向**——用户空闲多久后 AI 才被允许
接管。对「AI 拿到控制权后就不管了」没有任何约束，`_deadline` 也只是 1 秒级的输入
抑制心跳，不是交还超时。

现在加了 `agent_idle_release_seconds`（默认 120 秒）：每次 AI 动作刷新活动时间，
watchdog 每轮检查，超时就自动 `ready` 并通知界面撤掉叠加层。实测 200 秒无动作
触发交还，10 秒不触发。

**`IsolatedSession` 的 `stop` 只关宿主，不注销会话。**

这是最严重的一个。`stop` 写个停止标记让 RDP 宿主断开，然后看「会话里还有没有
active 的 explorer」——宿主一断，会话变 `disconnected`，这个判断立刻不满足，
于是报「子会话已关闭」并返回 `ok: true`。

但会话根本没死：`explorer`、`dwm`、`csrss`、`rdpclip` 三十多个进程还在跑，
每个占几百 MB。而且杀 explorer 三秒就自动重启，因为那是个完整的登录会话。

后果是反复 start/stop 会不断堆积孤儿会话。

现在 `stop` 走三步：让宿主断开 → **调 `WTSLogoffSession` 注销每个子会话** →
等会话从列表里真的消失。实测：

```
修复前  stop 返回 ok:true，会话 4 仍有 31 个进程
修复后  start 17.4s 建出 session 6（21 进程）
        stop 1.6s，消息「子会话已注销：[6]」
        会话彻底消失，零残留
```

**`Screenshot` 交还控制权后返回带光晕的旧帧。**

控制权在 AI 手上时，四周渐变光和顶部提示面板会画进截图；`release` 后那些装饰
消失，但帧哈希缓存还留着上一帧。下次截图如果画面碰巧命中旧帧，就会把带光晕的
旧图当成当前画面返回，看起来像光效卡死了。

第一次修的时候把清理放在 `if changed:` 里面，条件是 `state == "ai"`。实测发现
`release()` 被调用时 state 往往是 `unavailable` 或已经是 `ready`，changed 为
False，清理被跳过——而用户正是在这时候看到旧图。现在改成无条件执行，清一次
字典没有代价。实测 `cached_regions` 从 1 变成 0。

**`VirtualDesktop` 的 index 编号不一致。**

`list` 返回的 index 是 `range()` 给的 0 起，而 `target` 参数按 1 起解析。
「列表里第 1 个」和「target=1」指的不是同一个桌面，传错一次就把窗口挪到别处。

现在统一 1 起。同时修掉两处跟着 `+1` 的地方（错误提示里的桌面列表、
`windows` 动作返回的 `desktop_index`），否则会多算一次。实测 `index=1` 与
`_resolve(1)` 拿到同一个桌面。

### 顺带

`control.py` 加了 logger（之前没有，清缓存失败时无处记录）。

## 1.2.1

修 npm 包的污染。

### 修复

**1.2.0 的包里混进了 83 个 `.pyc`。** 它们散布在 `vendor/windows-mcp/` 下的
12 个 `__pycache__` 目录里，一共 1.6 MB。这不只是体积问题：`.pyc` 是 CPython 3.14
的字节码，用户环境如果是别的 Python 版本，这些陈旧字节码可能让导入出错。

根因是 npm 的优先级：`files` 字段 > `.npmignore` > `.gitignore`。
`dsh-bundle/package.json` 里 `files` 写了 `"vendor"`，npm 就把整个目录原样装进去，
`.gitignore` 里那条 `__pycache__/` 根本没被读到。

修法是在 `files` 里加排除条目：

```json
"files": [
  "vendor",
  "!vendor/**/__pycache__",
  "!vendor/**/*.pyc",
  "!vendor/**/*.pyo",
  ...
]
```

效果：

| | 1.2.0 | 1.2.1 |
|---|---|---|
| 包大小 | 925.8 kB | 317.9 kB |
| 解压后 | 3.0 MB | 1.4 MB |
| 文件数 | 187 | 104 |
| `.pyc` | 83 | 0 |

对了功能没影响，只是包干净了。另外补了一份 `.npmignore` 说明为什么它在这个包里
不生效，免得后来人以为改了它就能排掉文件。

## 1.2.0

新增独立子会话（Child Session）能力：AI 在另一个桌面里干活，**不抢用户的鼠标键盘**。
工具从 23 个压到 **15 个**。

### 新增

- **`IsolatedSession` 工具**。六个动作：
  - `status`：查当前能力——子会话状态、已存凭据、内置脚本运行时、能不能建会话
  - `setup`：弹 Windows 标准凭据框，用户输一次密码，`CryptProtectData` 加密后存进
    凭据管理器 `dsh-gui/Session`。之后不再问
  - `start`：起一个**常驻宿主进程**建子会话（默认 1280×720），等
    `explorer.exe` 出现才算就绪。实测 4.8 秒就绪，会话 `active`、协议 RDP。
    宿主是独立进程，插件重载不影响它——子会话的存活就绑在它身上
  - `stop`：让宿主断开，子会话随之销毁。实测 1.5 秒
  - `inject`：把 DLL 注入到子会话的 `explorer.exe`（同用户进程不需要
    `SeDebugPrivilege`）
  - `forget`：删掉存下的凭据

- **`desktop/child_session.py` 模块**。三条路线的实现与探测：
  - RDP 子会话：`ConnectToChildSession` + `ClearTextPassword`（实测 16 秒登录完成，
    `explorer.exe` 出现，会话 `Active`、协议 RDP、同一用户）
  - 注入：`OpenProcess` → `VirtualAllocEx` → `WriteProcessMemory` →
    `CreateRemoteThread(LoadLibraryW)`（实测在 283 个模块中确认新模块加载）
  - 脚本沙箱探测：找机器上已装的 node / python / pwsh，这条不需要密码

- **`desktop/frame_cache.py`**：帧哈希去重 + 暖 worker。
  - 帧哈希用来跳过重复帧的编码开销。1080p 实测 3.10ms（优化前 37.65ms）
  - 暖 worker 后台每 2s 空转截图，把 DWM flush / DXGI 枚举 / 相机对象保持热态，
    省掉第一次点击时几百毫秒的冷启动
  - 状态通过 `ControlStatus` 的 `capture` 字段暴露

### 变更

- **工具 23 → 15**。合并方式是不重写实现、只剥装饰器再套分发器：
  - `Click` / `Type` / `Scroll` / `Move` / `Shortcut` / `Wait` / `WaitFor`
    → 合并为 `Input`（`action` 分发）
  - `MultiSelect` / `MultiEdit` → 并入 `Input`
  - `CaptureStats` 的内容 → 并入 `ControlStatus` 的 `capture` 字段
  - 7 个实现函数一行未改，只是外面套了层壳

- **屏幕提示面板字体换等线**。原先是微软雅黑 Bold 24px，中文笔画发虚发灰；
  现在 `Dengb.ttf` 28px（标题）/ `Deng.ttf` 19px（正文），面板不透明度
  0.80 → 0.88。面板 398×112 → 424×118。本机 412 个字体里逐套渲染对比后选定。

### 常驻宿主

子会话的存活绑在 RDP 控件所在的进程上——控件所在的进程一退，会话立刻
`Disconnected`。所以 `start` 会起一个独立的 PowerShell 宿主进程
（`desktop/host_rdp.ps1`），由它持有 ActiveX 并跑消息泵。

关键设计：

- 宿主窗口挪到屏幕外 `(-32000, -32000)`，`ShowInTaskbar = false`，
  用户看不见它
- 宿主和插件是两个进程，插件重载 / 重启都不影响子会话
- 通过两个文件通信：`%TEMP%\dsh-gui-host-ready.txt`（就绪信号）、
  `%TEMP%\dsh-gui-host-stop.txt`（停止信号）
- 宿主自检连接状态，断了就自己退；最长存活 8 小时兜底

一个修过的坑：`find_child_explorer()` 一开始只看「会话里有没有
explorer.exe」，结果断开的会话（`state=4`）里 explorer 进程还在，
被误判成宿主在跑。现在要求会话状态必须是 `active` 或 `connected`。

### 为什么凭据要问用户一次

豆包工作（DoubaoWork）的做法是：从凭据管理器读一条已有的密码，用自定义的
AES-256 + SHA-512 方案解密，再通过命名管道明文发给 helper 进程。它从来不问用户。

**我们不照抄这一点。** 那条凭据是用户机器上现成的 Windows 密码，换台机器就不存在。
我们的做法是把同样的技术链路拆开，只替换凭据来源：

```
首次运行 → CredUIPromptForWindowsCredentials（Windows 标准框）
        → CryptProtectData（标准 DPAPI，本用户跨进程可解）
        → CredWriteW("dsh-gui/Session")
每次运行 → CredReadW + CryptUnprotectData → LogonUserW(INTERACTIVE) → primary token
```

存的是**标准 DPAPI blob**，不是自定义方案，便于调试和迁移。

### 已知约束

- **子会话的存活绑在 RDP 宿主进程上**。宿主一退，会话立刻 `Disconnected`。
  `start` 动作起的那个宿主就是干这个的，它是独立进程，插件重载不影响它。
- 一个系统同一时刻只能有一个活动子会话（微软文档）。
- `CreateProcessAsUser` 在本机不可用：缺 `SeAssignPrimaryToken`、`SeIncreaseQuota`、
  `SeTcb` 三个特权。所以走注入，不走新建进程——这也是豆包的做法。
- 能用 UIA 控件模式完成的操作**本来就不动真实指针**，应优先走那条路；
  子会话只解决 canvas / 自绘界面这类 UIA 覆盖不到的场景。

### 工程记录

调查与实测的完整过程记在 `docs/虚拟桌面计划.md`，包括：
- 微软文档说的「自动登录」在 PIN 登录的机器上不成立（未记录的第三个例外）
- `pip_main.exe` 的 IAT 精确解析（`LogonUserW` 在、`CredReadW` 不在）
- 账户锁定策略实测（10 次 / 600 秒）与批量验证的血泪教训
- 纯 Python 手工构造合法 PE 的方法（转发 DLL、最小可注入 DLL，都不需要编译器）
- `premultiplied_bgra()` 返回 BGRA 而不是 RGBA，预览时要把 R/B 换回来

## 1.1.1

包名去掉假 scope，改为无 scope 裸包名；按多面手的方式立项目。

### 变更

- **包名 `@local/gui` → `dsh-gui-forkgo`**。`@local` 不是真 scope，npm 上根本没有这个组织，
  纯粹是「本地私有包」的占位写法——好处是不与任何真包冲突，代价是永远发不出去。
  与「WPS 多面手」`dsh-multitool` 同为无 scope 裸包名。
  `forkgo` 后缀是因为 `dsh-gui` 已被 Caxson 预留（0.0.1，占名不发布，走 GitHub Releases）。
- **项目结构改为 monorepo**：根目录放文档与许可，`dsh-bundle/` 放可安装的 bundle，
  与 `wps-multitool-mcp/dsh-bundle/` 同构。
- **`dsh-bundle/package.json` 去掉 `exports`**。上游保持无 `exports` 字段时整包可访问，
  patch 里的 `createRequire.resolve('<包名>/package.json')` 反而不用额外放行；
  有 `exports` 就必须把 `'./package.json'` 列进去，否则 `ERR_PACKAGE_PATH_NOT_EXPORTED`。
- 补 `repository` / `homepage` / `bugs` / `keywords` / `engines`，对齐多面手的元数据形状。

### 修复

- **`PYTHONPATH` 从写死绝对路径改成 `!!js` 反解**。原来写的是本机
  `D:\EAGENT\DSH G1\bundles\windows-mcp-src`，别人 clone 下来装，这个目录不存在，
  而 dsh 不会报错——它会静默落回 PyPI 上游，于是 `VirtualDesktop` 消失、屏幕提示
  变回英文，看起来像功能没做。现在是「本包位置 + `vendor/windows-mcp`」。
- **源码树随包发布**，放在 `dsh-bundle/vendor/windows-mcp/` 并写进 `files`。
  之前它在本仓外面，npm pack 和 DSH 安装都不会带上。上游 MIT，
  版权声明随附在 `vendor/windows-mcp/LICENSE-upstream.md`。
- **`selftest.js` 不再从 patch 正则抠 `PYTHONPATH`**（那已经是个表达式），
  改为直接验 `vendor/` 目录本身；同时 `uvx` 的位置改成先查 PATH、再试常见安装点，
  不再写死 `C:\Users\金东权\...`。
- README 按去 AI 味的规则重写了一遍。保留全部数字、限制与结论，删掉没有信息量的
  装饰性排版、自我评价式小标题和反复用破折号制造的节奏。

## 1.1.0

（未单独发布，改动并入 1.1.1 的项目化）

- 加 `skills/gui-desktop/SKILL.md`，按多面手「薄壳包 + 外部 MCP + 一份 SKILL.md」补齐第三样。
- `cordis.patch.yml` 按 id 整行覆盖 `skill-filesystem`，`customSkillDirs` 追加本包 `skills/`。
- 加 `selftest.js`：验结构、技能 frontmatter、连接层三件事。


## 1.1.0

（未单独发布，改动并入 1.1.1 的项目化）

- 加 `skills/gui-desktop/SKILL.md`，按多面手「薄壳包 + 外部 MCP + 一份 SKILL.md」补齐第三样。
- `cordis.patch.yml` 按 id 整行覆盖 `skill-filesystem`，`customSkillDirs` 追加本包 `skills/`。
- 加 `selftest.js`：验结构、技能 frontmatter、连接层三件事。

## 1.0.0

首次落仓。

### 新增

- **`VirtualDesktop` 工具**：8 个 action。上游 `windows_mcp/vdm/core.py` 写好了全套
  虚拟桌面 COM 封装，但只在 `desktop/service.py:1416` 用来过滤窗口列表，
  21 个 MCP 工具里没有一个暴露它。
- **屏幕提示改中文**：蓝光边框 + 顶部面板 + 接管确认框，全部来自本包。
  `AI is controlling this computer` → `AI 正在控制这台电脑`。
- **字号压小**：上游 `range(24,7,-1)` 在 1920 屏上取 24 → 标题 32px、面板 500×160
  （占屏 26%×15%），像弹窗。上限改 16 → 标题 24px、面板 398×114（占屏 21%×11%）。

### 修复

- **上游封装的静默失败**：`switch_desktop` / `remove_desktop` / `move_window_to_desktop`
  出错只 `logger.error` 然后 `return None`，工具拿不到失败信号只能报成功。
  改为「解析 → 直接调 internal manager → 重读状态 → 对不上就抛」。
- **线程亲和**：`vdm.core` 的 manager 是 thread-local，`VirtualDesktopManagerInternal`
  属于 explorer.exe 的 apartment，而 MCP 同步工具体默认走 `asyncio.to_thread` 会散开。
  自持 STA 线程跑全部 COM，且**故意不用 UIA worker**——一次慢 `WaitFor`
  不该把切换桌面排在后面。
- **create 的返回名**：上游无名字时返回 `get_all_desktops()[-1]["name"]`，
  押「新的排最后」。改为一律按 GUID 回查。
- **中文字体**：`seguisb.ttf` / `segoeui.ttf` 没有汉字，直接换文案会出一排方框。
  标题 `msyhbd → Dengb → simhei → 西文`，正文 `msyh → Deng → simsun → 西文`；
  接管框走 Tk `font.families()` 探测。
- **快捷键黑底间距**：原来紧贴「接管」只隔 1px，现两侧各留 8px。

### 已确认的本机限制（Windows 10 19045）

- 无 `SetName`：vtable 里没有该方法，命名是 Win11 22000+ 能力。
- `CreateDesktopW` 不自动切换（Win11 会）。
- `MoveWindowToDesktop` 对本进程之外的窗口一律 `E_ACCESSDENIED`。

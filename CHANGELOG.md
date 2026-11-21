# Changelog

本项目的版本沿革。包名轨迹：`@local/windows-mcp` → `@local/gui` → **`dsh-gui-forkgo`**。

## 1.2.0

新增独立子会话（Child Session）能力：AI 在另一个桌面里干活，**不抢用户的鼠标键盘**。

### 新增

- **`IsolatedSession` 工具**（第 23 个）。四个动作：
  - `status`：查当前能力——子会话状态、已存凭据、内置脚本运行时、能不能建会话
  - `setup`：弹 Windows 标准凭据框，用户输一次密码，`CryptProtectData` 加密后存进
    凭据管理器 `dsh-gui/Session`。之后不再问
  - `inject`：把 DLL 注入到子会话的 `explorer.exe`（同用户进程不需要
    `SeDebugPrivilege`）
  - `forget`：删掉存下的凭据

- **`desktop/child_session.py` 模块**。三条路线的实现与探测：
  - RDP 子会话：`ConnectToChildSession` + `ClearTextPassword`（实测 16 秒登录完成，
    `explorer.exe` 出现，会话 `Active`、协议 RDP、同一用户）
  - 注入：`OpenProcess` → `VirtualAllocEx` → `WriteProcessMemory` →
    `CreateRemoteThread(LoadLibraryW)`（实测在 283 个模块中确认新模块加载）
  - 脚本沙箱探测：找机器上已装的 node / python / pwsh，这条不需要密码

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
  产品化必须有一个常驻的宿主。
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

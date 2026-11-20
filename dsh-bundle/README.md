# GUI（桌面与窗口控制）

> 作者：赢容 · MIT · 与「WPS 多面手」（`dsh-multitool`）同一位作者

DSH 的图形层：用 `@deepseek-ai/dsh-mcp-client` 起一个 stdio MCP 服务器 `windows-mcp`，把 Windows 的 UI Automation 和窗口管理包装成一组工具，模型就能像人一样看屏幕、点按钮、敲键盘、切换窗口、管理虚拟桌面。

工具名形如 `mcp__gui__<工具名>`（`serverName: gui`，共 22 个），另带一份技能 `gui-desktop` 教模型按顺序使用。

## 装

```powershell
# 本目录已经是完整 bundle，DSH 里走「插件 → 添加插件 → 本地目录」
# 或者 plugin_manager install_bundle，target 指到本目录的绝对路径
```

包名 `dsh-gui-forkgo`（无 scope，与「WPS 多面手」`dsh-multitool` 同一个风格）。
改名轨迹：`@local/windows-mcp` → `@local/gui` → **`dsh-gui-forkgo`**，旧包名都已不存在。
`suffix forkgo` 是为了避开 npm 上已被预留的 `dsh-gui`（作者 Caxson，占名不发布）。

## 提供什么

服务器名固定为 `gui`，21 个上游工具加 1 个本地新增的 `VirtualDesktop`：

| 能力 | 工具 |
|---|---|
| 观察 | `Snapshot`、`Screenshot`、`DisplayInventory` |
| 鼠标 | `Click`、`Move`、`Scroll`、`MultiSelect` |
| 键盘 | `Type`、`Shortcut`、`MultiEdit` |
| 窗口与程序 | `App`、`Process`、`Wait`、`WaitFor` |
| 文件与系统 | `FileSystem`、`Registry`、`PowerShell`、`Clipboard`、`Scrape`、`Notification` |
| 控制权 | `ControlStatus` |
| 虚拟桌面 | `VirtualDesktop`（任务栏上那几组窗口：列出、新建、切换、重命名、删除、把窗口挪过去） |

和内置 `pwsh` 工具的分工：`pwsh` 是命令行层（改文件、跑脚本、抓日志），GUI 是图形层（点安装向导、读弹窗、处理只有 GUI 才有的流程）。

## 依赖

- `uvx` 在 PATH 上（本机：`%USERPROFILE%\.local\bin\uvx.exe`）
- 依赖由 uvx 从 PyPI 装（`windows-mcp` 要求 Python `>=3.14`：pywin32、comtypes、dxcam 等）

## 本地分支怎么接的

**包本体来自包内 vendor 的源码，依赖仍来自 PyPI。**

```yaml
args: ['--python','3.14','--from','windows-mcp','windows-mcp','serve','--transport','stdio']
env:
  PYTHONPATH: <本包位置>/vendor/windows-mcp   # !!js 反解，不写死
```

不用 `--from <本地目录>`：uvx 对本地目录的构建缓存不看文件 mtime，改完源码它照样复用上一次构建的产物。实测把整个文案改成中文之后跑起来还是旧文案，而且没有任何提示。`PYTHONPATH` 让源码直接生效，改一行就是一行。

代价是那个目录必须是**全量源码树**（完整的 `windows_mcp/` 包），不能是只放改动文件的覆盖层。PYTHONPATH 一旦命中，同名子模块全部从这里取，缺文件就是 ImportError。

路径由 `!!js` 从本包位置反解，不写死绝对路径。写死的话别人 clone 下来装，那个目录不存在，而 dsh 不会报错——它静默落回 PyPI 上游，于是 VirtualDesktop 消失、屏幕提示变回英文。

源码里的改动与实测边界见 `vendor/windows-mcp/README.local.md`。

## 改

- 换服务器网关名：`cordis.patch.yml` 里的 `serverName`（工具前缀随之变化，规则 `^[A-Za-z0-9_-]{1,32}$`）
- 退回纯上游：删掉 `PYTHONPATH` 那段即可（22 个工具会退回 21 个，`VirtualDesktop` 消失）
- 命令跑得慢：调 `toolCallTimeoutMs` / `startupTimeoutMs`
- 不想自动重连：`reconnect.enabled: false`

改完在插件页重新启用 `include:gui` 即可热加载。

## 自检

改完先跑一遍，不需要 dsh：

```powershell
node selftest.js              # 结构 + 技能 + 连接层（会真起一次 MCP，约 5 秒）
node selftest.js --no-connect # 只验结构与技能
```

验的是三件「改对了也看不出来」的事：

1. **包结构** —— `dsh.bundle.patch`、`exports['./package.json']`、`args` 里没有相对路径、
   `PYTHONPATH` 指向的是完整源码树而不是增量覆盖层。
2. **技能 frontmatter** —— `name` 是 kebab-case 且与目录名一致、`description` 不为空。
   缺 description 时 dsh 会静默不加载，用户只看到「AI 不会用」，没有任何报错。
3. **连接层** —— 真起 stdio、`initialize`、`tools/list`，确认 22 个工具里
   `VirtualDesktop` 在（本地分支生效）、屏幕提示文案是中文、工具名合 dsh 字符集。

## 提交

本目录是独立 git 仓库，署名与「WPS 多面手」一致：

```powershell
git config user.name  "赢容"
git config user.email "yingrong@local"
```

提交信息用 `feat:` / `fix:` / `chore:` + 中文描述，`——` 分隔动作与理由。


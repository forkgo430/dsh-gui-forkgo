# GUI · DSH 的图形层

> 作者：赢容 · MIT · 与 [WPS 多面手](https://github.com/forkgo430/wps-multitool-mcp)（`dsh-multitool`）同一位作者

给 [DeepSeek Harness](https://github.com/deepseek-ai/dsh) 装上眼睛和手：截屏观察、点击、输入、快捷键、窗口与进程、文件、注册表、PowerShell、剪贴板、虚拟桌面。

**22 个工具**（`mcp__gui__*`）+ **一份技能** `gui-desktop`（教模型按什么顺序用）。

---

## 它解决什么

DSH 自带的 `pwsh` 是命令行层：改文件、跑脚本、抓日志都行。
但**只有图形界面才有的流程**——安装向导、登录弹窗、没有 CLI 的老软件、要拖拽的界面——命令行碰不到。

GUI 补的就是这一层。

## 装

```powershell
# 方式一：DSH 插件页 → 添加插件 → 本地目录，指到本仓的 dsh-bundle/
# 方式二：plugin_manager install_bundle，target 指到 dsh-bundle/ 的绝对路径
```

依赖 `uvx`（[uv](https://docs.astral.sh/uv/)）在 PATH 上，首次启动自动拉 Python 3.14 和 pywin32、comtypes、dxcam。

## 提供什么

| 能力 | 工具 | 说明 |
|---|---|---|
| 观察 | `Snapshot` · `Screenshot` · `DisplayInventory` | 控件树带可点坐标；`use_vision=False` 时纯文本不附图 |
| 鼠标 | `Click` · `Move` · `Scroll` · `MultiSelect` | 按坐标或按控件；拖拽、滚轮、多选 |
| 键盘 | `Type` · `Shortcut` · `MultiEdit` | 输入、快捷键、批量填多格 |
| 窗口与程序 | `App` · `Process` · `Wait` · `WaitFor` | 启动、切换、调整窗口、等条件成立 |
| 文件与系统 | `FileSystem` · `Registry` · `PowerShell` · `Clipboard` · `Scrape` · `Notification` | |
| 控制权 | `ControlStatus` | 谁在控制、空闲多久交还 |
| **虚拟桌面** | **`VirtualDesktop`** | 任务栏那几组窗口：列出、新建、切换、重命名、删除、搬窗口 |

### 两个值得说的点

**一、`Snapshot` 默认不附图。** 只返回控件树（角色、名称、坐标、可用动作、快捷键），比截图便宜得多，而且坐标直接可点。只有 canvas、WebGL、图片里的文字才需要开 `use_vision`。

**二、`VirtualDesktop` 是本仓新增的。** 上游 `windows-mcp` 把虚拟桌面的 COM 封装写好了，却只用来过滤窗口列表，21 个工具里没有一个暴露它。这里挂成工具，并修掉上游封装三处会让工具说谎的毛病（静默失败、线程亲和、返回值靠假设）。

## 屏幕提示

工具接管桌面时，屏幕四周会有一圈蓝光、顶部中间一块提示、接管前一个确认框。**都是中文**：

```
┌──────────────────────────────┐
│    AI 正在控制这台电脑        │
│      大幅移动鼠标             │
│  或按 Ctrl+Alt+Shift+Backspace 接管 │
└──────────────────────────────┘
```

两种接管方式：大幅移动鼠标，或按 `Ctrl + Alt + Shift + Backspace`。
提示面板在 1920 屏上约 398×114（占屏 21%×11%），窄屏自动缩到 276×97。

## 边界

**这不是输入隔离。** 切换虚拟桌面只隔离窗口，不隔离输入——鼠标键盘还是共享的，被切走的桌面里进程照跑、照出声。要真正隔离得用 Terminal Server 子会话，那是另一个量级的东西。

**Windows 10 19045 实测的三条硬限制**（工具里如实拒绝，不假装成功）：

| 限制 | 表现 |
|---|---|
| 不支持桌面命名 | `rename` / `create(name=)` 抛 `NotImplementedError`（命名是 Win11 22000+ 能力） |
| 新建不自动切换 | `create` 返回 `switched:false`（Win11 会切） |
| `move_window` 只搬本进程窗口 | 别的进程的窗口抛 `PermissionError`，本版本无可用绕行 |

第三条排查过 `IApplicationViewCollection`（`QueryService` 返回 `E_NOTIMPL`）、`Shift+Win+方向`（忽略合成输入）、各种指针封送写法（同一错误）。不是权限问题——同一 token、同一会话，自己的窗口能搬。

## 结构

```
dsh-gui-forkgo/
├── dsh-bundle/          ← 装进 DSH 的就是这个目录
│   ├── package.json
│   ├── cordis.patch.yml 挂 MCP 客户端 + 技能扫描根
│   ├── skills/
│   │   └── gui-desktop/SKILL.md
│   ├── locale/{zh,en}.json
│   ├── docs/DETAIL.md
│   └── selftest.js      结构 + 技能 + 连接层自检
├── docs/
├── package.json
└── LICENSE
```

`dsh-bundle/` 是薄壳：只有配置与资产，不含可执行代码。真正干活的是 `uvx` 拉起的 `windows-mcp` 子进程。

## 自检

改完先跑一遍，不需要 dsh：

```powershell
npm run selftest           # 结构 + 技能 + 连接层（真起一次 MCP，约 5 秒）
npm run selftest:offline   # 只验结构与技能
```

它卡三件「改对了也看不出来」的事：

1. **包结构** —— `dsh.bundle.patch`、`args` 里没有相对路径、`PYTHONPATH` 指向完整源码树。
2. **技能 frontmatter** —— `name` 是 kebab-case 且与目录名一致、`description` 不为空。
   缺 description 时 dsh 会静默不加载，用户只看到「AI 不会用」，没有任何报错。
3. **连接层** —— 真起 stdio、列工具、确认 22 个里 `VirtualDesktop` 在、屏幕文案是中文。

## 与上游的关系

包本体基于 [`windows-mcp`](https://pypi.org/project/windows-mcp/) 0.8.9 的本地分支，
通过 `PYTHONPATH` 覆盖，依赖仍由 uvx 从 PyPI 装。

为什么不用 `uvx --from <本地目录>`：uvx 对本地目录的构建缓存**不跟文件 mtime 走**，
改完源码它照样复用上一次构建的产物。实测把整份文案改成中文之后跑起来还是英文，
而且没有任何提示。`PYTHONPATH` 让源码直接生效。

## 许可

MIT。见 [LICENSE](LICENSE)。

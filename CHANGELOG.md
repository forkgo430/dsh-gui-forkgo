# Changelog

本项目的版本沿革。包名轨迹：`@local/windows-mcp` → `@local/gui` → **`dsh-gui-forkgo`**。

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

- 上一版把 `PYTHONPATH` 的坑写进注释却没写进文档，这次在 README「与上游的关系」里说明白：
  uvx 对本地目录的构建缓存不跟 mtime 走，改完源码仍复用旧产物且无任何提示。

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

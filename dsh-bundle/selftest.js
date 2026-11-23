/**
 * Input: 无（可选 GUI_MCP_PYTHONPATH 覆盖源码树位置）
 * Output: 结构自检 + 技能自检 + 连接层自检；失败 exit 1
 * Pos: dsh-bundle 的自检脚本。**不需要 dsh**，只验三件 bundle 最容易坏的事：
 *      ① 包结构符合 dsh 的 bundle 规范；② skills/ 里那份技能能被 dsh 认出来；
 *      ③ windows-mcp 起得来、8 个工具列得出来、名字合 dsh 的字符集。
 *
 * 用法：node selftest.js
 *       node selftest.js --no-connect   （只跑结构+技能，不启动 MCP 子进程）
 *
 * 为什么需要它：这个 bundle 有三处是"改对了也看不出来"的坑——
 *   - cordis.patch.yml 里 args 写成相对路径，发布后必然找不到；
 *   - SKILL.md 的 frontmatter 缺 description，dsh 静默不加载，用户只看到"AI 不会用"；
 *   - serverName 改掉后工具前缀跟着变，旧提示词里的 mcp__gui__* 全部失效。
 * 三条都在这脚本里卡住。
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';

const HERE = dirname(fileURLToPath(import.meta.url));
const NO_CONNECT = process.argv.includes('--no-connect');
let failed = 0;
const ok = (msg) => console.log(`  ✓ ${msg}`);
const bad = (msg) => {
  failed++;
  console.error(`  ✗ ${msg}`);
};
const note = (msg) => console.log(`  · ${msg}`);

/** 找 uvx：先看 GUI_UVX 覆盖，再按 PATH，最后试几个常见安装点。 */
function resolveUvx() {
  if (process.env.GUI_UVX && existsSync(process.env.GUI_UVX)) return process.env.GUI_UVX;

  const exe = process.platform === 'win32' ? 'uvx.exe' : 'uvx';
  for (const dir of (process.env.PATH ?? '').split(process.platform === 'win32' ? ';' : ':')) {
    if (!dir) continue;
    const candidate = join(dir, exe);
    if (existsSync(candidate)) return candidate;
  }

  for (const guess of [
    join(process.env.USERPROFILE ?? '', '.local', 'bin', exe),
    join(process.env.HOME ?? '', '.local', 'bin', exe),
    '/usr/local/bin/uvx',
    '/usr/bin/uvx',
  ]) {
    if (guess && existsSync(guess)) return guess;
  }
  return '';
}

// ---------------------------------------------------------------- 1. 结构

console.log('结构：');

const pkg = JSON.parse(readFileSync(join(HERE, 'package.json'), 'utf8'));

if (pkg.dsh?.bundle?.patch === './cordis.patch.yml') ok('package.json 声明了 dsh.bundle.patch');
else bad(`dsh.bundle.patch 不对：${JSON.stringify(pkg.dsh?.bundle?.patch)}`);

if (pkg.author) ok(`author = ${pkg.author}`);
else bad('缺 author');

if (pkg.license) ok(`license = ${pkg.license}`);
else bad('缺 license');

// dsh 靠包名反解本 bundle 的位置（patch 里的 !!js 用 createRequire.resolve）。
// 有 exports 字段时它会成为白名单，没列 './package.json' 就 ERR_PACKAGE_PATH_NOT_EXPORTED；
// 没有 exports 则整包可访问，反而更简单。两种都行，但必须能解析到。
if (pkg.exports && !pkg.exports['./package.json']) {
  bad("有 exports 但没暴露 './package.json'（patch 里解析自身位置会失败）");
} else if (pkg.exports) {
  ok("exports 暴露了 './package.json'");
} else {
  ok('无 exports 字段（整包可访问，反解自身没问题）');
}

const patchPath = join(HERE, 'cordis.patch.yml');
if (!existsSync(patchPath)) {
  bad('cordis.patch.yml 不存在');
} else {
  ok('cordis.patch.yml 存在');
  const patch = readFileSync(patchPath, 'utf8');

  for (const [needle, label] of [
    ["name: '@deepseek-ai/dsh-mcp-client'", '挂了官方 MCP 客户端'],
    ['serverName: gui', 'serverName = gui（工具前缀 mcp__gui__*）'],
    ['- id: skill-filesystem', '启用了 skill-filesystem'],
    ['- id: tool-skill', '启用了 tool-skill'],
  ]) {
    if (patch.includes(needle)) ok(label);
    else bad(`patch 里缺：${label}`);
  }

  // 发布后相对路径必然是错的
  if (/args:\s*\n\s*-\s*['"]?\.\.?\//.test(patch)) bad('patch 的 args 里出现了相对路径');
  else ok('args 里没有相对路径');

  // command 也不能写死绝对路径。
  //
  // 原先是 'C:\Users\<开发者>\.local\bin\uvx.exe' —— 只在开发机上成立。别人装上以后
  // 这个文件不存在，spawn 直接 ENOENT，而 dsh 只报一句
  // 「initial connection or tool synchronization failed」，看不出是缺了 uv。
  // 现在必须由 !!js 探测（用户目录 / UV_INSTALL_DIR / LOCALAPPDATA / cargo / chocolatey）。
  const commandLine = patch.match(/^\s*command:.*$/m)?.[0] ?? '';
  if (/^\s*command:\s*['"]?[A-Za-z]:[\\/]/.test(commandLine)) {
    bad(`command 被写死成绝对路径，换台机器就 spawn 失败：${commandLine.trim()}`);
  } else if (/^\s*command:\s*!!js/.test(commandLine)) {
    ok('command 由 !!js 探测（可移植）');
  } else if (commandLine) {
    note(`command 是裸名或其它形式，确认一下：${commandLine.trim()}`);
  } else {
    bad('patch 里找不到 command');
  }

  // --from 必须钉版本。
  //
  // 裸 `windows-mcp` 在开发机上一直没事，因为 uv 早就缓存了当时的解析结果；换台干净
  // 机器就会现拉最新版。上游小版本会动 CLI 参数和依赖图，钉住才能复现验证过的行为。
  // vendor 里的源码树是 0.8.9，Pin 也必须对齐它。
  const fromLine = patch.match(/^\s*-\s*['"]?(windows-mcp[^'"\n]*)['"]?\s*$/m)?.[1] ?? '';
  if (/windows-mcp==\d+\.\d+\.\d+/.test(fromLine)) {
    ok(`--from 钉了版本：${fromLine}`);
    const pinned = fromLine.split('==')[1];
    if (pinned !== '0.8.9') {
      note(`钉的是 ${pinned}，但 vendor 源码树是 0.8.9 —— 升级时两处一起改`);
    }
  } else if (fromLine) {
    bad(`--from 没钉版本（${fromLine}）：干净机器会拉最新版，行为和这里不同`);
  }

  // PYTHONPATH 是本地分支的挂载点。它由 !!js 从本包位置反解出来
  // （../vendor/windows-mcp），不是写死的绝对路径 —— 写死的话别人 clone 下来装，
  // 路径不存在，dsh 不会报错而是静默落回 PyPI 上游：VirtualDesktop 消失、
  // 屏幕提示变回英文，看起来像功能没做。
  //
  // 所以这里不能去 patch 里正则抠路径（那是个表达式），直接验 vendor 目录本身。
  if (/PYTHONPATH:/.test(patch)) {
    if (/PYTHONPATH:\s*'[A-Za-z]:[\\/]/.test(patch)) {
      bad('PYTHONPATH 被写成了绝对路径，别人 clone 下来会失效');
    } else if (/PYTHONPATH:\s*!!js/.test(patch)) {
      ok('PYTHONPATH 由 !!js 反解（可移植）');
    } else {
      note('PYTHONPATH 既不是 !!js 也不是绝对路径，确认一下是不是故意的');
    }

    const vendor = join(HERE, 'vendor', 'windows-mcp');
    if (!existsSync(vendor)) {
      bad(`vendor 源码树不存在：${vendor}`);
    } else if (!existsSync(join(vendor, 'windows_mcp', '__init__.py'))) {
      bad('vendor 里缺 windows_mcp/__init__.py，不是完整源码树');
    } else {
      ok('vendor/windows-mcp 是完整源码树');

      // 本分支的两处改动必须在，否则静默退回上游行为
      const art = join(vendor, 'windows_mcp', 'desktop', 'control_overlay_art.py');
      if (existsSync(art) && readFileSync(art, 'utf8').includes('AI 正在控制这台电脑')) {
        ok('vendor 里屏幕提示是中文');
      } else {
        bad('vendor 里的屏幕提示还是英文');
      }

      if (existsSync(join(vendor, 'windows_mcp', 'tools', 'virtual_desktop.py'))) {
        ok('vendor 里有 virtual_desktop.py');
      } else {
        bad('vendor 里缺 virtual_desktop.py');
      }

      // 上游 MIT，随附许可证是硬要求
      if (existsSync(join(vendor, 'LICENSE-upstream.md'))) ok('vendor 随附上游 LICENSE');
      else bad('vendor 缺 LICENSE-upstream.md（MIT 要求保留版权声明）');
    }
  } else {
    note('patch 里没有 PYTHONPATH，将使用 PyPI 上游版本');
  }
}

// ---------------------------------------------------------------- 2. 技能

console.log('技能：');

const skillsRoot = join(HERE, 'skills');
if (!existsSync(skillsRoot)) {
  bad('skills/ 不存在');
} else {
  const dirs = readdirSync(skillsRoot, { withFileTypes: true }).filter((d) => d.isDirectory());
  if (!dirs.length) bad('skills/ 下没有子目录');

  for (const d of dirs) {
    const md = join(skillsRoot, d.name, 'SKILL.md');
    if (!existsSync(md)) {
      bad(`${d.name}/SKILL.md 不存在`);
      continue;
    }
    const text = readFileSync(md, 'utf8');
    const fm = /^---\r?\n([\s\S]*?)\r?\n---/.exec(text);
    if (!fm) {
      bad(`${d.name}: SKILL.md 没有 YAML frontmatter`);
      continue;
    }
    const name = /^name:\s*(.+)$/m.exec(fm[1])?.[1]?.trim();
    const desc = /^description:\s*(.+)$/m.exec(fm[1])?.[1]?.trim();

    if (name && /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(name)) ok(`${d.name}: name = ${name}（kebab-case 合规）`);
    else bad(`${d.name}: name 不合规：${name ?? '(缺失)'}`);

    if (name && name !== d.name) bad(`${d.name}: name(${name}) 与目录名不一致，dsh 按目录名索引`);
    else if (name) ok(`${d.name}: name 与目录名一致`);

    if (desc && desc.length > 20) ok(`${d.name}: description ${desc.length} 字`);
    else bad(`${d.name}: description 缺失或过短（dsh 加载技能时必填）`);
  }
}

// ------------------------------------------------------------- 3. 连接层

if (NO_CONNECT) {
  console.log('连接层：（已跳过）');
} else {
  console.log('连接层：');
  await checkConnect();
}

async function checkConnect() {
  // patch 里的 PYTHONPATH 是 !!js 表达式，这里不去解析它，按同一个约定算：
  // 包根 + 'vendor/windows-mcp'。
  const vendor = join(HERE, 'vendor', 'windows-mcp');
  const pythonPath =
    process.env.GUI_MCP_PYTHONPATH || (existsSync(vendor) ? vendor : '');

  if (!pythonPath) {
    note('没找到 vendor 源码树，连接层将只验 PyPI 上游（VirtualDesktop 与文案检查会跳过）');
  }

  // uvx 的位置各机器不同：先在 PATH 上找，找不到再试常见安装点。
  const uvx = resolveUvx();
  if (!uvx) {
    bad('找不到 uvx。装一个：https://docs.astral.sh/uv/');
    return;
  }
  note(`uvx: ${uvx}`);

  const child = spawn(
    uvx,
    ['--python', '3.14', '--from', 'windows-mcp', 'windows-mcp', 'serve', '--transport', 'stdio'],
    {
      env: {
        ...process.env,
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8',
        UV_NO_PROGRESS: '1',
        ...(pythonPath ? { PYTHONPATH: pythonPath } : {}),
      },
      stdio: ['pipe', 'pipe', 'pipe'],
    },
  );

  let buf = '';
  let settled = false;
  const pending = new Map();
  const started = Date.now();

  child.stdout.on('data', (chunk) => {
    buf += chunk.toString('utf8');
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch {
        continue;
      }
      const waiter = pending.get(msg.id);
      if (waiter) {
        pending.delete(msg.id);
        waiter(msg);
      }
    }
  });

  const stderrTail = [];
  child.stderr.on('data', (c) => {
    stderrTail.push(c.toString('utf8'));
    if (stderrTail.length > 20) stderrTail.shift();
  });

  const send = (id, method, params) => {
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
  };
  const wait = (id, ms) =>
    new Promise((resolve) => {
      const t = setTimeout(() => resolve(null), ms);
      pending.set(id, (msg) => {
        clearTimeout(t);
        resolve(msg);
      });
    });

  const cleanup = () => {
    if (settled) return;
    settled = true;
    try {
      child.stdin.end();
    } catch {}
    try {
      child.kill();
    } catch {}
  };

  try {
    send(1, 'initialize', {
      protocolVersion: '2024-11-05',
      capabilities: {},
      clientInfo: { name: 'gui-selftest', version: pkg.version },
    });
    const init = await wait(1, 90000);
    if (!init?.result) {
      bad('initialize 无响应（uvx 冷启动可能要 90s，先手动跑一次预热）');
      if (stderrTail.length) note(stderrTail.join('').slice(-400));
      return;
    }
    ok(`initialize 通过（${Date.now() - started}ms）`);

    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');

    send(2, 'tools/list', {});
    const list = await wait(2, 30000);
    const tools = list?.result?.tools ?? [];
    if (!tools.length) {
      bad('工具数为 0：server 起来了但没列工具');
      return;
    }
    // 期望工具数。改合并方式时同步改这里；CI 的 docs job 从这一行读数字，
    // 拿它去核对 README 有没有写漂，省得同一个数字散落在三处各改一遍。
    const EXPECTED_TOOL_COUNT = 8;
    if (tools.length !== EXPECTED_TOOL_COUNT) {
      bad(`工具数 ${tools.length}，期望 ${EXPECTED_TOOL_COUNT}`
          + '：多了少了都说明合并或注册出了问题');
    } else {
      ok(`工具数：${tools.length}`);
    }

    const names = tools.map((t) => t.name);
    const illegal = names.filter((n) => !/^[A-Za-z0-9_-]+$/.test(n));
    if (illegal.length) bad(`工具名不合 dsh 字符集：${illegal.slice(0, 3).join(', ')}`);
    else ok('工具名全部合 dsh 的字符集 [A-Za-z0-9_-]');

    const tooLong = names.filter((n) => `mcp__gui__${n}`.length > 64);
    if (tooLong.length) bad(`加前缀后超 64 字符：${tooLong.slice(0, 3).join(', ')}`);
    else ok('加 mcp__gui__ 前缀后全部 ≤ 64 字符');

    // 15 → 8 合并后的完整名单。少一个说明注册表漏了，多一个说明旧模块又被加回来了。
    // VirtualDesktop 在不在，同时验证了 PYTHONPATH 是否生效 —— 它只存在于本地分支，
    // 掉了就说明跑的是 PyPI 上游版。
    for (const expected of [
      'ControlStatus', 'Screen', 'Input', 'Programs',
      'System', 'Scrape', 'VirtualDesktop', 'IsolatedSession',
    ]) {
      if (names.includes(expected)) ok(`${expected} 在`);
      else bad(`${expected} 不在：tools/__init__.py 未注册或模块导入失败`);
    }

    // 被合并掉的旧工具名必须彻底消失。
    // 这一条是防复发的关键：旧模块文件还留在 tools/ 里（为了让上游单元测试还能
    // import 到符号），只要有人一时手快把 app / process / snapshot 加回 _MODULES，
    // 工具数会变成 15 混着 8，而**不会报任何错**。这里直接把它们钉死。
    for (const retired of [
      'Snapshot', 'Screenshot', 'DisplayInventory', 'App', 'Process',
      'PowerShell', 'FileSystem', 'Registry', 'Clipboard',
    ]) {
      if (names.includes(retired)) {
        bad(`旧工具 ${retired} 又出现了：它已被合并进 Screen / Programs / System / Input`);
      }
    }
    ok('9 个旧工具名都没重现（合并彻底）');

    // 合并后每个工具的 mode/action 必须真的能分发。只看 schema 里有没有这个参数
    // 是不够的 —— 参数存在但分支漏了，调用时才炸。这里检查枚举值。
    const enumOf = (toolName, prop) => {
      const tool = tools.find((t) => t.name === toolName);
      return tool?.inputSchema?.properties?.[prop]?.enum
        ?? tool?.parameters?.properties?.[prop]?.enum
        ?? tool?.parameters?.properties?.[prop]?.items?.enum
        ?? [];
    };

    const screenModes = enumOf('Screen', 'mode');
    for (const m of ['full', 'fast', 'displays']) {
      if (screenModes.length && screenModes.includes(m)) ok(`Screen.mode 有 ${m}`);
      else if (screenModes.length) bad(`Screen.mode 缺 ${m}（现有：${screenModes.join(',')}）`);
    }

    const programModes = enumOf('Programs', 'mode');
    for (const m of ['launch', 'launch_executable', 'resize', 'switch', 'list', 'kill']) {
      if (programModes.length && programModes.includes(m)) ok(`Programs.mode 有 ${m}`);
      else if (programModes.length) bad(`Programs.mode 缺 ${m}`);
    }

    const systemModes = enumOf('System', 'mode');
    for (const m of ['read', 'write', 'copy', 'move', 'delete', 'list', 'search', 'info',
                     'reg_get', 'reg_set', 'reg_delete', 'reg_list', 'shell']) {
      if (systemModes.length && systemModes.includes(m)) ok(`System.mode 有 ${m}`);
      else if (systemModes.length) bad(`System.mode 缺 ${m}`);
    }

    // 工具名不能重复——重名会让后注册的那个被静默覆盖
    const dups = names.filter((n, i) => names.indexOf(n) !== i);
    if (dups.length) bad(`工具名重复：${[...new Set(dups)].join(', ')}`);
    else ok('工具名无重复');

    // 控制权分类必须覆盖所有工具。
    // 这条是最容易被漏掉的：control_policy.py 里没登记的工具会被当成 direct，
    // 于是只读的 Screen 也去抢用户的鼠标键盘。不报错，只是行为变差。
    if (pythonPath) {
      const policyPath = join(pythonPath, 'windows_mcp', 'tools', 'control_policy.py');
      if (existsSync(policyPath)) {
        const src = readFileSync(policyPath, 'utf8');
        const missing = names.filter((n) => n !== 'ControlStatus'
          && !new RegExp(`["']${n}["']`).test(src));
        if (missing.length) {
          bad(`control_policy.py 没登记这些工具（会默认成 direct 抢控制权）：${missing.join(', ')}`);
        } else {
          ok('control_policy.py 覆盖了全部 8 个工具');
        }
      } else {
        note('找不到 control_policy.py，跳过分类覆盖检查');
      }
    }

    // 屏幕提示必须已是中文；这是本分支的改动之一
    if (pythonPath) {
      const art = join(pythonPath, 'windows_mcp', 'desktop', 'control_overlay_art.py');
      if (existsSync(art)) {
        const text = readFileSync(art, 'utf8');
        if (text.includes('AI 正在控制这台电脑')) ok('屏幕提示文案是中文');
        else bad('屏幕提示文案还是英文（control_overlay_art.py 未覆盖）');
      } else {
        note('找不到 control_overlay_art.py，跳过文案检查');
      }
    }
  } catch (e) {
    bad(`连接失败：${e.message}`);
  } finally {
    cleanup();
  }
}

console.log(failed === 0 ? '\n全部通过' : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);

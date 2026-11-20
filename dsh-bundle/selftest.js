/**
 * Input: 无（可选 GUI_MCP_PYTHONPATH 覆盖源码树位置）
 * Output: 结构自检 + 技能自检 + 连接层自检；失败 exit 1
 * Pos: dsh-bundle 的自检脚本。**不需要 dsh**，只验三件 bundle 最容易坏的事：
 *      ① 包结构符合 dsh 的 bundle 规范；② skills/ 里那份技能能被 dsh 认出来；
 *      ③ windows-mcp 起得来、22 个工具列得出来、名字合 dsh 的字符集。
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

  // PYTHONPATH 是本地分支挂载点，必须指到存在的目录
  const m = /PYTHONPATH:\s*'([^']+)'/.exec(patch);
  if (m) {
    if (existsSync(m[1])) ok(`PYTHONPATH 指向存在：${m[1]}`);
    else bad(`PYTHONPATH 指向不存在：${m[1]}`);
    if (existsSync(join(m[1], 'windows_mcp', '__init__.py'))) ok('PYTHONPATH 是完整源码树（有 windows_mcp/）');
    else bad('PYTHONPATH 不是完整源码树（缺 windows_mcp/__init__.py）');
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
  // 和 patch 里保持一致：uvx + PyPI 包 + PYTHONPATH 覆盖源码
  const patch = existsSync(join(HERE, 'cordis.patch.yml'))
    ? readFileSync(join(HERE, 'cordis.patch.yml'), 'utf8')
    : '';
  const pythonPath =
    process.env.GUI_MCP_PYTHONPATH || /PYTHONPATH:\s*'([^']+)'/.exec(patch)?.[1] || '';

  const child = spawn(
    'C:\\Users\\金东权\\.local\\bin\\uvx.exe',
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
    ok(`工具数：${tools.length}`);

    const names = tools.map((t) => t.name);
    const illegal = names.filter((n) => !/^[A-Za-z0-9_-]+$/.test(n));
    if (illegal.length) bad(`工具名不合 dsh 字符集：${illegal.slice(0, 3).join(', ')}`);
    else ok('工具名全部合 dsh 的字符集 [A-Za-z0-9_-]');

    const tooLong = names.filter((n) => `mcp__gui__${n}`.length > 64);
    if (tooLong.length) bad(`加前缀后超 64 字符：${tooLong.slice(0, 3).join(', ')}`);
    else ok('加 mcp__gui__ 前缀后全部 ≤ 64 字符');

    // 本 bundle 的存在理由就是这个工具；掉了就说明 PYTHONPATH 没生效、跑的是上游
    if (names.includes('VirtualDesktop')) ok('VirtualDesktop 在（本地分支生效）');
    else bad('VirtualDesktop 不在：PYTHONPATH 未生效，跑的是 PyPI 上游 21 工具版');

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

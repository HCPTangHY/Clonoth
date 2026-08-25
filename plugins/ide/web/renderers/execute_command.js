// execute_command 工具卡内容渲染器。
// 槽位：tool_card_content:execute_command。data 是宿主给的工具快照
// { toolName, arguments, result, error, status, elapsedMs, nodeId }。
//
// 命令可视化（纯视觉变换，不执行任何逻辑）：
// - 按 && || ; | 分段，每段一行，连接符显示在段首
// - 每段首词按命令类别着色（导航/检索/读写/版本控制/运行时/网络/构建）
// - 重定向尾缀（>、2>&1）折叠为灰色标签
// - 超时以内联标签显示在命令块头

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ));
}

// ── 命令类别着色 ───────────────────────────────────────────────────────
// 同类命令同色，颜色取自宿主配色语义的扩展：关键字紫（操作类）、字符串绿
//（读/检索类）、蓝（导航/移动）、橙（运行时/构建）、红（写/删除）、灰（其他）。
const CMD_CLASSES = [
  ['nav',      /^(cd|ls|pwd|tree|pushd|popd)\b/,          '#4078f2'],
  ['read',     /^(cat|head|tail|less|more|grep|rg|find|sed|awk|wc|diff|sort|uniq|which|file|stat|du|df)\b/, '#50a14f'],
  ['vcs',      /^(git)\b/,                                 '#986801'],
  ['runtime',  /^(python3?|node|npx|npm|pnpm|yarn|bun|deno|ruby|go|cargo|java|bash|sh|zsh|tsc|esbuild)\b/, '#e45649'],
  ['net',      /^(curl|wget|ping|ssh|scp|rsync|nc)\b/,     '#0184bc'],
  ['write',    /^(cp|mv|mkdir|touch|rm|rmdir|ln|chmod|chown|tar|zip|unzip|install)\b/, '#c18401'],
  ['build',    /^(make|cmake|docker|docker-compose|systemctl|service|pm2)\b/, '#a626a4'],
];

function cmdColor(seg) {
  const first = (seg.trim().match(/^[^\s]+/) || [''])[0];
  for (const [, re, color] of CMD_CLASSES) {
    if (re.test(first)) return color;
  }
  return null;
}

// ── 分段：按 && || ; | 切分，保留连接符 ──────────────────────────────
// 不解析引号——视觉分段允许误切，误切的最坏结果是多一行，不影响语义展示。
function splitCommand(cmd) {
  const segs = [];
  let cur = '';
  let i = 0;
  while (i < cmd.length) {
    const two = cmd.slice(i, i + 2);
    if (two === '&&' || two === '||') {
      if (cur.trim()) segs.push({ op: segs.length ? '·' : '', text: cur.trim() });
      cur = '';
      i += 2;
      continue;
    }
    const ch = cmd[i];
    if (ch === ';' || (ch === '|' && cmd[i + 1] !== '|')) {
      if (cur.trim()) segs.push({ op: segs.length ? (ch === '|' ? '|' : ';') : '', text: cur.trim() });
      cur = '';
      i += 1;
      continue;
    }
    cur += ch;
    i += 1;
  }
  if (cur.trim()) segs.push({ op: segs.length ? '·' : '', text: cur.trim() });
  return segs;
}

// ── 重定向折叠：> file、2>&1、>> file 提取为标签 ────────────────────
function foldRedirects(seg) {
  const tags = [];
  const text = seg.replace(/\s*(2>>?|>>?|&>)([^\s]+)/g, (_, op, target) => {
    tags.push(op + (target === '&1' ? '&1' : ' ' + target));
    return '';
  }).trim();
  return { text, tags };
}

function renderCommand(cmd, timeoutSec) {
  const segs = splitCommand(cmd);
  let html = '<div class="ide-tc-cmd">';
  html += '<div class="ide-tc-cmdhead"><span>命令</span>';
  if (timeoutSec != null) html += '<span class="ide-tc-timeout">' + esc(timeoutSec) + 's</span>';
  html += '</div>';
  for (const seg of segs) {
    const { text, tags } = foldRedirects(seg.text);
    const color = cmdColor(text);
    const first = (text.match(/^[^\s]+/) || [''])[0];
    const rest = text.slice(first.length);
    html += '<div class="ide-tc-seg">';
    if (seg.op) html += '<span class="ide-tc-op">' + esc(seg.op) + '</span>';
    html += '<code>';
    html += color ? '<span style="color:' + color + ';font-weight:600">' + esc(first) + '</span>' : esc(first);
    html += esc(rest);
    html += '</code>';
    for (const t of tags) html += '<span class="ide-tc-redir">' + esc(t) + '</span>';
    html += '</div>';
  }
  html += '</div>';
  return html;
}

function kv(label, value) {
  if (value === undefined || value === null || value === '') return '';
  return '<div class="ide-tc-kv"><span class="ide-tc-k">' + esc(label) + '</span>' +
    '<span class="ide-tc-v">' + esc(value) + '</span></div>';
}

function render(ctx) {
  const d = ctx.data || {};
  const args = d.arguments || {};
  const parts = [];

  if (args.command) {
    parts.push(renderCommand(String(args.command), args.timeout_sec));
  }

  const meta = kv('工作目录', args.workdir || '')
    + kv('节点', d.nodeId || '')
    + kv('耗时', d.elapsedMs != null ? (d.elapsedMs / 1000).toFixed(1) + 's' : '');
  if (meta) parts.push('<div class="ide-tc-block">' + meta + '</div>');

  const out = d.error || d.result || '';
  if (out) {
    const cls = d.error ? 'ide-tc-code ide-tc-err' : 'ide-tc-code';
    parts.push('<div class="ide-tc-block"><div class="ide-tc-label">' +
      (d.error ? '错误' : '输出') + '</div><pre class="' + cls + '">' + esc(out) + '</pre></div>');
  }

  ctx.el.innerHTML = parts.join('');
}

export default {
  mount(ctx) { render(ctx); },
  update(ctx) { render(ctx); },
};

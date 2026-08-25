// execute_command 工具卡内容渲染器。
// 槽位：tool_card_content:execute_command。data 是宿主给的工具快照
// { toolName, arguments, result, error, status, elapsedMs, nodeId }。
//
// 命令可视化（纯视觉变换，不执行任何逻辑）：
// - 引号状态机：单双引号与反斜杠转义内的分隔符不切分，heredoc 等内嵌
//   脚本保持为一段
// - 按 && || ; | 分段，每段一行，连接符显示在段首
// - 每段首词按命令类别着色
// - 重定向尾缀折叠为灰色标签
// - 块头右侧"分段/原文"切换，状态存 ctx.state 跨更新保留

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ));
}

const CMD_CLASSES = [
  ['nav',     /^(cd|ls|pwd|tree|pushd|popd)\b/,          '#4078f2'],
  ['read',    /^(cat|head|tail|less|more|grep|rg|find|sed|awk|wc|diff|sort|uniq|which|file|stat|du|df)\b/, '#50a14f'],
  ['vcs',     /^(git)\b/,                                 '#986801'],
  ['runtime', /^(python3?|node|npx|npm|pnpm|yarn|bun|deno|ruby|go|cargo|java|bash|sh|zsh|tsc|esbuild)\b/, '#e45649'],
  ['net',     /^(curl|wget|ping|ssh|scp|rsync|nc)\b/,     '#0184bc'],
  ['write',   /^(cp|mv|mkdir|touch|rm|rmdir|ln|chmod|chown|tar|zip|unzip|install)\b/, '#c18401'],
  ['build',   /^(make|cmake|docker|docker-compose|systemctl|service|pm2)\b/, '#a626a4'],
];

function cmdColor(seg) {
  const first = (seg.trim().match(/^[^\s]+/) || [''])[0];
  for (const [, re, color] of CMD_CLASSES) {
    if (re.test(first)) return color;
  }
  return null;
}

// 引号与 heredoc 感知的分段。规则：
// 1. 单双引号、反斜杠转义内的 &&/||/;/| 不切
// 2. heredoc（<< 'TAG' 或 << TAG 或 <<- TAG）识别后，从 heredoc 开始到
//    对应结束标记行的全部内容保持为一段
// 3. 引号未闭合时剩余内容全部留在当前段
function splitCommand(cmd) {
  // 预处理：识别 heredoc 范围，用占位符替换其中的分隔符
  const heredocRanges = [];
  const heredocRe = /<<-?\s*['"]?(\w+)['"]?/g;
  let hm;
  while ((hm = heredocRe.exec(cmd)) !== null) {
    const tag = hm[1];
    // 找结束标记：单独一行的 TAG
    const endRe = new RegExp('\\n' + tag + '\\b', 'g');
    endRe.lastIndex = hm.index + hm[0].length;
    const endMatch = endRe.exec(cmd);
    if (endMatch) {
      heredocRanges.push({ start: hm.index, end: endMatch.index + endMatch[0].length });
    }
  }

  const inHeredoc = (pos) => heredocRanges.some(r => pos >= r.start && pos < r.end);
  // heredoc 结束位置之后应有分段边界
  const heredocEnds = new Set(heredocRanges.map(r => r.end));

  const segs = [];
  let cur = '';
  let i = 0;
  let quote = null; // null | "'" | '"'
  while (i < cmd.length) {
    const ch = cmd[i];
    // heredoc 范围内的所有字符原样保留，不做任何分段判断
    if (inHeredoc(i)) {
      cur += ch;
      i += 1;
      // heredoc 刚结束，强制断段
      if (heredocEnds.has(i) && cur.trim()) {
        segs.push({ op: segs.length ? '·' : '', text: cur.trim() });
        cur = '';
      }
      continue;
    }
    if (quote) {
      cur += ch;
      if (ch === '\\' && quote === '"' && i + 1 < cmd.length) {
        cur += cmd[i + 1];
        i += 2;
        continue;
      }
      if (ch === quote) quote = null;
      i += 1;
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      cur += ch;
      i += 1;
      continue;
    }
    if (ch === '\\' && i + 1 < cmd.length) {
      cur += ch + cmd[i + 1];
      i += 2;
      continue;
    }
    const two = cmd.slice(i, i + 2);
    if (two === '&&' || two === '||') {
      if (cur.trim()) segs.push({ op: segs.length ? '·' : '', text: cur.trim() });
      cur = '';
      i += 2;
      continue;
    }
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

function foldRedirects(seg) {
  const tags = [];
  const text = seg.replace(/\s*(2>>?|>>?|&>)([^\s]+)/g, (_, op, target) => {
    tags.push(op + (target === '&1' ? '&1' : ' ' + target));
    return '';
  }).trim();
  return { text, tags };
}

function renderSegs(cmd) {
  const segs = splitCommand(cmd);
  let html = '';
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
  const state = ctx.state || (ctx.state = {});
  const parts = [];

  if (args.command) {
    const cmd = String(args.command);
    const raw = !!state.cmdRaw;
    let block = '<div class="ide-tc-cmd"><div class="ide-tc-cmdhead"><span>命令</span>' +
      '<span class="ide-tc-cmdhead-right">';
    if (args.timeout_sec != null) block += '<span class="ide-tc-timeout">' + esc(args.timeout_sec) + 's</span>';
    block += '<button type="button" class="ide-tc-cmdtoggle">' + (raw ? '分段' : '原文') + '</button>';
    block += '</span></div>';
    block += raw
      ? '<pre class="ide-tc-code ide-tc-cmdraw">' + esc(cmd) + '</pre>'
      : renderSegs(cmd);
    block += '</div>';
    parts.push(block);
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

  const toggle = ctx.el.querySelector('.ide-tc-cmdtoggle');
  if (toggle) {
    toggle.addEventListener('click', () => {
      state.cmdRaw = !state.cmdRaw;
      render(ctx);
    });
  }
}

export default {
  mount(ctx) { render(ctx); },
  update(ctx) { render(ctx); },
};

// grep / search_in_files 工具卡内容渲染器。
// 槽位：tool_card_content:grep、tool_card_content:search_in_files。
// 参数：pattern 高亮 + path/glob 键值。结果：按 "file:line | match" 解析为
// 分组列表，文件名可点击在 IDE 打开；解析失败回退原文本块。

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ));
}

function kv(label, value) {
  if (value === undefined || value === null || value === '') return '';
  return '<div class="ide-tc-kv"><span class="ide-tc-k">' + esc(label) + '</span>' +
    '<span class="ide-tc-v">' + esc(value) + '</span></div>';
}

// 解析结果文本为 { file: [{line, match, context}] } 分组
function parseMatches(text) {
  const groups = [];
  let cur = null;
  for (const line of text.split('\n')) {
    const m = line.match(/^(.+?):(\d+) \| (.*)$/);
    if (m) {
      if (!cur || cur.file !== m[1]) {
        cur = { file: m[1], items: [] };
        groups.push(cur);
      }
      cur.items.push({ line: m[2], match: m[3], context: '' });
    } else if (cur && /^\s+\S/.test(line)) {
      // 上下文行
      const last = cur.items[cur.items.length - 1];
      if (last) last.context += (last.context ? '\n' : '') + line.trim();
    }
  }
  return groups;
}

function render(ctx) {
  const d = ctx.data || {};
  const args = d.arguments || {};
  ctx.el.innerHTML = '';

  // ── 参数 ──
  const pattern = args.query || args.pattern || '';
  let head = '<div class="ide-tc-block">';
  if (pattern) head += '<div class="ide-tc-pattern"><code>' + esc(pattern) + '</code></div>';
  head += kv('路径', args.path || '') + kv('过滤', args.pattern && args.query ? args.pattern : (args.glob || ''));
  head += '</div>';
  const headEl = document.createElement('div');
  headEl.innerHTML = head;
  ctx.el.appendChild(headEl.firstChild);

  // ── 结果 ──
  const out = d.error || d.result || '';
  if (!out) return;
  if (d.error) {
    const pre = document.createElement('pre');
    pre.className = 'ide-tc-code ide-tc-err';
    pre.textContent = out;
    ctx.el.appendChild(pre);
    return;
  }

  const groups = parseMatches(out);
  if (!groups.length) {
    const pre = document.createElement('pre');
    pre.className = 'ide-tc-code';
    pre.textContent = out;
    ctx.el.appendChild(pre);
    return;
  }

  const summary = document.createElement('div');
  summary.className = 'ide-tc-label';
  summary.textContent = out.split('\n')[0]; // "N results found..."
  ctx.el.appendChild(summary);

  for (const g of groups) {
    const gEl = document.createElement('div');
    gEl.className = 'ide-tc-grep-group';
    const fileBtn = document.createElement('button');
    fileBtn.type = 'button';
    fileBtn.className = 'ide-tc-grep-file';
    fileBtn.textContent = g.file;
    fileBtn.title = '在 IDE 打开';
    fileBtn.addEventListener('click', () => {
      void ctx.api?.call?.('openPanel', 'files', { kind: 'open-file', path: g.file });
    });
    gEl.appendChild(fileBtn);
    for (const item of g.items) {
      const row = document.createElement('div');
      row.className = 'ide-tc-grep-row';
      row.innerHTML = '<span class="ide-tc-grep-line">' + esc(item.line) + '</span>' +
        '<code class="ide-tc-grep-match">' + esc(item.match) + '</code>';
      gEl.appendChild(row);
    }
    ctx.el.appendChild(gEl);
  }
}

export default {
  mount(ctx) { render(ctx); },
  update(ctx) { render(ctx); },
};

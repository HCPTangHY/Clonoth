// read_file 工具卡内容渲染器。
// 槽位：tool_card_content:read_file。
// 参数：文件列表（path + 行范围）渲染为可点击 chip，点击在 IDE 打开。
// 结果：按 "── path ──" 分节，每节一个代码块；分节解析失败回退原文本块。

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ));
}

function render(ctx) {
  const d = ctx.data || {};
  const args = d.arguments || {};
  ctx.el.innerHTML = '';

  // ── 参数：文件列表 ──
  const files = Array.isArray(args.files) && args.files.length
    ? args.files
    : (args.path ? [{ path: args.path, startLine: args.startLine, endLine: args.endLine }] : []);
  if (files.length) {
    const wrap = document.createElement('div');
    wrap.className = 'ide-tc-files';
    for (const f of files) {
      if (!f || typeof f !== 'object') continue;
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'ide-tc-filechip';
      const range = (f.startLine || f.endLine)
        ? ':' + (f.startLine || '') + (f.endLine && f.endLine !== f.startLine ? '-' + f.endLine : '')
        : '';
      chip.textContent = String(f.path || '?') + range;
      chip.title = '在 IDE 打开';
      chip.addEventListener('click', () => {
        void ctx.api?.call?.('openPanel', 'files', { kind: 'open-file', path: String(f.path || '') });
      });
      wrap.appendChild(chip);
    }
    ctx.el.appendChild(wrap);
  }

  // ── 结果：按 ── path ── 分节 ──
  const out = d.error || d.result || '';
  if (!out) return;
  const sections = [];
  const re = /^── (.+?) ──$/gm;
  let m; const marks = [];
  while ((m = re.exec(out)) !== null) marks.push({ path: m[1], start: m.index, headEnd: m.index + m[0].length });
  if (marks.length) {
    for (let i = 0; i < marks.length; i++) {
      const end = i + 1 < marks.length ? marks[i + 1].start : out.length;
      sections.push({ path: marks[i].path, body: out.slice(marks[i].headEnd, end).replace(/^\n+|\n+$/g, '') });
    }
    for (const sec of sections) {
      const block = document.createElement('div');
      block.className = 'ide-tc-block';
      block.innerHTML = '<div class="ide-tc-label">' + esc(sec.path) + '</div>' +
        '<pre class="ide-tc-code">' + esc(sec.body) + '</pre>';
      ctx.el.appendChild(block);
    }
  } else {
    const block = document.createElement('div');
    block.className = 'ide-tc-block';
    block.innerHTML = '<div class="ide-tc-label">' + (d.error ? '错误' : '结果') + '</div>' +
      '<pre class="ide-tc-code' + (d.error ? ' ide-tc-err' : '') + '">' + esc(out) + '</pre>';
    ctx.el.appendChild(block);
  }
}

export default {
  mount(ctx) { render(ctx); },
  update(ctx) { render(ctx); },
};

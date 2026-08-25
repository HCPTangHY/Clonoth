// list_dir 工具卡内容渲染器。
// 槽位：tool_card_content:list_dir。
// 参数：目录路径 chip。结果：目录树文本块（按 ── path ── 分节）。

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ));
}

function render(ctx) {
  const d = ctx.data || {};
  const args = d.arguments || {};
  ctx.el.innerHTML = '';

  const dirs = Array.isArray(args.paths) && args.paths.length
    ? args.paths
    : (args.path ? [args.path] : (args.directory ? [args.directory] : []));
  if (dirs.length) {
    const wrap = document.createElement('div');
    wrap.className = 'ide-tc-files';
    for (const p of dirs) {
      const chip = document.createElement('span');
      chip.className = 'ide-tc-filechip ide-tc-dirchip';
      chip.textContent = String(p);
      wrap.appendChild(chip);
    }
    ctx.el.appendChild(wrap);
  }

  const out = d.error || d.result || '';
  if (!out) return;
  const block = document.createElement('div');
  block.className = 'ide-tc-block';
  block.innerHTML = '<pre class="ide-tc-code ide-tc-tree' + (d.error ? ' ide-tc-err' : '') + '">' + esc(out) + '</pre>';
  ctx.el.appendChild(block);
}

export default {
  mount(ctx) { render(ctx); },
  update(ctx) { render(ctx); },
};

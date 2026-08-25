// execute_command 工具卡内容渲染器。
// 槽位：tool_card_content:execute_command。data 是宿主给的工具快照
// { toolName, arguments, result, error, status, elapsedMs, nodeId }。
// 分块布局：命令 code 块 / 参数键值 / 输出 transcript。

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

function render(ctx) {
  const d = ctx.data || {};
  const args = d.arguments || {};
  const parts = [];

  // 命令本体
  if (args.command) {
    parts.push('<div class="ide-tc-block"><div class="ide-tc-label">命令</div>' +
      '<pre class="ide-tc-code">' + esc(args.command) + '</pre></div>');
  }

  // 执行参数键值
  const meta = kv('超时', args.timeout_sec != null ? args.timeout_sec + 's' : '')
    + kv('工作目录', args.workdir || '')
    + kv('节点', d.nodeId || '')
    + kv('耗时', d.elapsedMs != null ? (d.elapsedMs / 1000).toFixed(1) + 's' : '');
  if (meta) parts.push('<div class="ide-tc-block">' + meta + '</div>');

  // 输出 / 错误
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

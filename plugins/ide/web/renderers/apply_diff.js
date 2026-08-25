// apply_diff 工具卡内容渲染器。
// 槽位：tool_card_content:apply_diff。把 arguments.diffs 数组渲染为红绿
// 对比块，并提供"在 IDE 打开"按钮跳到变更文件（走 openPanel 通用动作 +
// ide 私有的 open-file 意图）。

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ));
}

function renderDiffBlock(diff) {
  const before = String(diff.search || '').replace(/\n$/, '').split('\n');
  const after = String(diff.replace || '').replace(/\n$/, '').split('\n');
  let html = '<div class="ide-tc-diff">';
  for (const line of before) {
    html += '<div class="ide-tc-del">- ' + esc(line || ' ') + '</div>';
  }
  for (const line of after) {
    html += '<div class="ide-tc-add">+ ' + esc(line || ' ') + '</div>';
  }
  html += '</div>';
  return html;
}

function render(ctx) {
  const d = ctx.data || {};
  const args = d.arguments || {};
  const diffs = Array.isArray(args.diffs) ? args.diffs : [];
  const path = String(args.path || '');

  ctx.el.innerHTML = '';

  // 文件头 + IDE 打开按钮
  if (path) {
    const head = document.createElement('div');
    head.className = 'ide-tc-filehead';
    const label = document.createElement('code');
    label.textContent = path;
    head.appendChild(label);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ide-tc-openbtn';
    btn.textContent = '在 IDE 打开';
    btn.addEventListener('click', () => {
      void ctx.api?.call?.('openPanel', 'files', { kind: 'open-file', path });
    });
    head.appendChild(btn);
    ctx.el.appendChild(head);
  }

  // diff 块
  if (diffs.length) {
    for (const diff of diffs) {
      const wrap = document.createElement('div');
      wrap.innerHTML = renderDiffBlock(diff);
      ctx.el.appendChild(wrap.firstChild);
    }
  } else if (d.argumentsText) {
    const pre = document.createElement('pre');
    pre.className = 'ide-tc-code';
    pre.textContent = d.argumentsText;
    ctx.el.appendChild(pre);
  }

  // 结果/错误
  const out = d.error || d.result || '';
  if (out) {
    const pre = document.createElement('pre');
    pre.className = d.error ? 'ide-tc-code ide-tc-err' : 'ide-tc-code';
    pre.textContent = out;
    ctx.el.appendChild(pre);
  }
}

export default {
  mount(ctx) { render(ctx); },
  update(ctx) { render(ctx); },
};

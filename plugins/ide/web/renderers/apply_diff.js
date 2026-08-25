// apply_diff 工具卡内容渲染器。
// 槽位：tool_card_content:apply_diff。
//
// diff 渲染：把 arguments.diffs 的 search/replace 做行级对齐——相同前缀与
// 后缀行渲染为上下文（无底色），中间差异段红绿对比。这让"只改了三行"
// 和"整个函数重写"在视觉上一眼可辨。
//
// "在 IDE 打开"按钮走 openPanel 通用动作 + ide 私有 open-file 意图。

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ));
}

// 行级对齐：相同前缀/后缀行作为上下文，中间差异段红绿对比。
function renderAlignedDiff(search, replace) {
  const a = String(search || '').replace(/\n$/, '').split('\n');
  const b = String(replace || '').replace(/\n$/, '').split('\n');

  // 共同前缀
  let pre = 0;
  while (pre < a.length && pre < b.length && a[pre] === b[pre]) pre++;
  // 共同后缀（不与前缀重叠）
  let suf = 0;
  while (suf < a.length - pre && suf < b.length - pre && a[a.length - 1 - suf] === b[b.length - 1 - suf]) suf++;

  const ctxBefore = a.slice(0, pre);
  const delLines = a.slice(pre, a.length - suf);
  const addLines = b.slice(pre, b.length - suf);
  const ctxAfter = a.slice(a.length - suf);

  let html = '<div class="ide-tc-diff">';
  for (const line of ctxBefore) html += '<div class="ide-tc-ctx">  ' + esc(line || ' ') + '</div>';
  for (const line of delLines) html += '<div class="ide-tc-del">- ' + esc(line || ' ') + '</div>';
  for (const line of addLines) html += '<div class="ide-tc-add">+ ' + esc(line || ' ') + '</div>';
  for (const line of ctxAfter) html += '<div class="ide-tc-ctx">  ' + esc(line || ' ') + '</div>';
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
      // open-diff intent：面板页打开 diff 标签，读文件后应用 diffs 渲染
      // 修改前/修改后的行级对比。diffs 数据随 intent 传递（不透明对象）。
      void ctx.api?.call?.('openPanel', 'files', {
        kind: 'open-diff', path, diffs: args.diffs || [],
      });
    });
    head.appendChild(btn);
    ctx.el.appendChild(head);
  }

  // diff 块
  if (diffs.length) {
    for (const diff of diffs) {
      const wrap = document.createElement('div');
      wrap.innerHTML = renderAlignedDiff(diff.search, diff.replace);
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

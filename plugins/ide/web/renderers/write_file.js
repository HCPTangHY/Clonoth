// write_file 工具卡内容渲染器。
// 槽位：tool_card_content:write_file。
// 参数：路径（可点击在 IDE 打开）+ 字节数 + 内容预览（超过 40 行折叠）。
// 结果：简单文本行。

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ));
}

const PREVIEW_LINES = 40;

function render(ctx) {
  const d = ctx.data || {};
  const args = d.arguments || {};
  ctx.el.innerHTML = '';

  const path = String(args.path || '');
  const content = String(args.content || '');

  if (path) {
    const head = document.createElement('div');
    head.className = 'ide-tc-filehead';
    const label = document.createElement('code');
    label.textContent = path;
    head.appendChild(label);
    const size = document.createElement('span');
    size.className = 'ide-tc-k';
    size.textContent = content ? content.length + ' 字符' : '';
    head.appendChild(size);
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

  if (content) {
    const lines = content.split('\n');
    const long = lines.length > PREVIEW_LINES;
    const pre = document.createElement('pre');
    pre.className = 'ide-tc-code';
    pre.textContent = long ? lines.slice(0, PREVIEW_LINES).join('\n') : content;
    ctx.el.appendChild(pre);
    if (long) {
      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'ide-tc-openbtn ide-tc-more';
      more.textContent = '展开全部 ' + lines.length + ' 行';
      more.addEventListener('click', () => {
        pre.textContent = content;
        pre.classList.remove('ide-tc-clamp');
        more.remove();
      });
      pre.classList.add('ide-tc-clamp');
      ctx.el.appendChild(more);
    }
  }

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

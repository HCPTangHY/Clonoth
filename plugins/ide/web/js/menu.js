/* 右键菜单：面板内通用上下文菜单组件，文件树与标签栏共用。 */

let ctxMenuEl = null;
let ctxMenuCloser = null;

function closeContextMenu() {
  if (!ctxMenuEl) return;
  if (ctxMenuCloser) { ctxMenuCloser(); ctxMenuCloser = null; }
  ctxMenuEl.remove();
  ctxMenuEl = null;
}

/**
 * 在 (x, y) 处弹出菜单。items: [{ label, title?, danger?, action() }]。
 * 空数组返回 false，调用方应回落浏览器默认菜单。
 */
function showContextMenu(x, y, items) {
  if (!items || !items.length) return false;
  closeContextMenu();
  const menu = document.createElement('div');
  menu.className = 'ctx-menu';
  for (const it of items) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ctx-item' + (it.danger ? ' danger' : '');
    btn.textContent = it.label;
    if (it.title) btn.title = it.title;
    btn.addEventListener('click', () => {
      closeContextMenu();
      it.action();
    });
    menu.appendChild(btn);
  }
  document.body.appendChild(menu);
  ctxMenuEl = menu;

  // 视口边界：超出右/下沿时回移
  const rect = menu.getBoundingClientRect();
  let left = x, top = y;
  if (left + rect.width > window.innerWidth - 4) left = Math.max(4, window.innerWidth - rect.width - 4);
  if (top + rect.height > window.innerHeight - 4) top = Math.max(4, window.innerHeight - rect.height - 4);
  menu.style.left = left + 'px';
  menu.style.top = top + 'px';

  const onDown = (ev) => {
    if (!ctxMenuEl) return;
    if (ctxMenuEl.contains(ev.target)) return;
    closeContextMenu();
  };
  const onKey = (ev) => { if (ev.key === 'Escape') closeContextMenu(); };
  const onWin = () => closeContextMenu();
  document.addEventListener('pointerdown', onDown, true);
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('blur', onWin);
  window.addEventListener('scroll', onWin, true);
  ctxMenuCloser = () => {
    document.removeEventListener('pointerdown', onDown, true);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('blur', onWin);
    window.removeEventListener('scroll', onWin, true);
  };
  return true;
}

// 复制到剪贴板。iframe 内 clipboard API 可能被权限拦截，带回退方案。
function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
  } else {
    fallbackCopy(text);
  }
}

function fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } catch (e) { /* 忽略 */ }
  ta.remove();
}

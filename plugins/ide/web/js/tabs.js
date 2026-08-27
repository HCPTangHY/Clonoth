/* 标签状态与标签栏：tab 对象构造、预览/固定语义、打开/关闭/切换、标签栏渲染。 */

// ── tab state ────────────────────────────────────────────────────────────
// tabs: [{ path, pinned, loaded, kind, text, savedText, dirty, scroll,
//          blobUrl, note }]。path === TREE_TAB 是文件树标签，永远在首位。
// 文件标签中至多一个未固定（预览标签）；编辑预览标签会自动将其固定。

let tabs = [{ path: TREE_TAB, pinned: true }];
let activePath = TREE_TAB;
let viewPath = null;      // 当前视图为哪个标签渲染，用于切换前保存滚动位置
let saveHint = '';        // 状态栏一次性提示（保存成功/失败）

function newFileTab(path, pinned) {
  // mode: MD 文件专属，'render'（默认渲染）或 'edit'（编辑器）。
  // scroll 存比例（scrollTop / 可滚动高度）而非像素：渲染与编辑两种模式
  // 的滚动容器与内容高度都不同，像素值无法对应。cursor 存编辑态游标位置。
  return { path, pinned, loaded: false, kind: '', text: '', savedText: '',
           dirty: false, scroll: 0, cursor: 0, blobUrl: null, note: '', mode: 'render',
           diffs: null, afterText: '' };
}

function newDiffTab(path, diffs) {
  return { path, pinned: true, loaded: false, kind: 'diff', text: '', savedText: '',
           dirty: false, scroll: 0, cursor: 0, blobUrl: null, note: '', mode: 'render',
           diffs, afterText: '' };
}

function scrollFraction(el) {
  const range = el.scrollHeight - el.clientHeight;
  return range > 0 ? el.scrollTop / range : 0;
}

function applyScrollFraction(el, frac) {
  const range = el.scrollHeight - el.clientHeight;
  el.scrollTop = range > 0 ? frac * range : 0;
}

function findTab(path) {
  return tabs.find((t) => t.path === path) || null;
}

function findPreviewTab() {
  return tabs.find((t) => t.path !== TREE_TAB && !t.pinned) || null;
}

function releaseTab(tab) {
  if (tab && tab.blobUrl) { URL.revokeObjectURL(tab.blobUrl); tab.blobUrl = null; }
}

function activate(path) {
  if (activePath === path) return;
  activePath = path;
  renderTabs();
  renderView();
}

function openFileTab(path, pinned) {
  let tab = findTab(path);
  if (tab) {
    if (pinned) tab.pinned = true;
  } else if (pinned) {
    tab = newFileTab(path, true);
    tabs.push(tab);
  } else {
    tab = newFileTab(path, false);
    const preview = findPreviewTab();
    if (preview) {
      releaseTab(preview);
      tabs[tabs.indexOf(preview)] = tab;
    } else {
      tabs.push(tab);
    }
  }
  activePath = path;
  renderTabs();
  renderView();
}

function openDiffTab(path, diffs) {
  // diff 标签页 key 带 ' (diff)' 后缀，与同名文件标签区分。
  const key = path + ' (diff)';
  let tab = findTab(key);
  if (tab) {
    tab.diffs = diffs;
    tab.loaded = false;
  } else {
    tab = newDiffTab(key, diffs);
    tabs.push(tab);
  }
  activePath = key;
  renderTabs();
  renderView();
}

function closeTab(path) {
  if (path === TREE_TAB) return;
  const tab = findTab(path);
  if (!tab) return;
  if (tab.dirty && !confirm('未保存的修改将丢失，确定关闭 ' + baseName(path) + '？')) return;
  const idx = tabs.indexOf(tab);
  releaseTab(tab);
  tabs.splice(idx, 1);
  if (activePath === path) {
    const next = tabs[Math.min(idx, tabs.length - 1)] || tabs[0];
    activePath = next.path;
  }
  renderTabs();
  renderView();
}

function renderTabs() {
  // [AutoC 2026-08-24] 不能 innerHTML 清空整个 tabbar：右端的 bar-end
  // 保存按钮容器会被销毁，导致按钮永久消失。只移除标签元素，bar-end 保留。
  tabbarEl.querySelectorAll('.tab').forEach((el) => el.remove());
  for (const tab of tabs) {
    const isTree = tab.path === TREE_TAB;
    const el = document.createElement('div');
    el.className = 'tab'
      + (tab.path === activePath ? ' active' : '')
      + (!isTree && !tab.pinned ? ' preview-tab' : '')
      + (tab.dirty ? ' dirty' : '');
    el.title = isTree ? '工作区文件' : tab.path;
    const nm = document.createElement('span');
    nm.className = 'nm';
    nm.textContent = isTree ? '文件' : baseName(tab.path);
    el.appendChild(nm);
    if (!isTree) {
      const dot = document.createElement('span');
      dot.className = 'dot';
      dot.textContent = '●';
      el.appendChild(dot);
      const close = document.createElement('button');
      close.className = 'close';
      close.type = 'button';
      close.textContent = '✕';
      close.title = '关闭';
      close.addEventListener('click', (e) => {
        e.stopPropagation();
        closeTab(tab.path);
      });
      el.appendChild(close);
      el.addEventListener('auxclick', (e) => {
        if (e.button === 1) closeTab(tab.path);
      });
      el.addEventListener('dblclick', () => {
        if (!tab.pinned) { tab.pinned = true; renderTabs(); }
      });
      // [AutoC 2026-08-27] 右键菜单：固定/关闭/关闭其他/关闭右侧/复制路径。
      // 只挂在文件标签上，文件树标签回落浏览器默认菜单。
      el.addEventListener('contextmenu', (e) => {
        const items = [];
        items.push(tab.pinned
          ? { label: '取消固定', action: () => { tab.pinned = false; renderTabs(); } }
          : { label: '固定标签', action: () => { tab.pinned = true; renderTabs(); } });
        items.push({ label: '关闭', action: () => closeTab(tab.path) });
        const idx = tabs.indexOf(tab);
        const others = tabs.filter((t) => t.path !== TREE_TAB && t.path !== tab.path);
        if (others.length) {
          items.push({
            label: '关闭其他',
            action: () => { for (const t of others) closeTab(t.path); },
          });
        }
        const rights = tabs.slice(idx + 1).filter((t) => t.path !== TREE_TAB);
        if (rights.length) {
          items.push({
            label: '关闭右侧',
            action: () => { for (const t of rights.slice().reverse()) closeTab(t.path); },
          });
        }
        items.push({
          label: '复制路径',
          action: () => copyText(String(tab.path).replace(/ \(diff\)$/, '')),
        });
        e.preventDefault();
        e.stopPropagation();
        showContextMenu(e.clientX, e.clientY, items);
      });
    }
    el.addEventListener('click', () => activate(tab.path));
    tabbarEl.insertBefore(el, tabbarEl.querySelector('.bar-end'));
  }
}

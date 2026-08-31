/* 工作区内容搜索：整页视图（与 Git 整页视图同一层级）。
 *
 * 入口是文件树路径栏的搜索按钮（buildSearchButton，bootTree 挂载）。
 * 视图与文件树/Git 视图互斥：激活时暂存文件树 DOM，关闭时恢复。
 * 后端 GET /v1/plugins/ide/search（grep 子进程实现）返回按文件分组的
 * 匹配列表；点击匹配打开文件（固定标签）并滚动定位到对应行。 */

const SEARCH_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>';

let searchViewActive = false;
let searchTreeBackup = null;
let searchResults = null;   // { query, total, truncated, files }
let searchLoading = false;
let searchSeq = 0;          // 过期响应丢弃

function buildSearchButton() {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'git-btn';
  btn.title = '在工作区搜索';
  btn.innerHTML = SEARCH_ICON;
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    openSearchView('');
  });
  return btn;
}

async function openSearchView(prefill) {
  if (activePath !== TREE_TAB) {
    activePath = TREE_TAB;
    renderTabs();
  }
  // 与 Git 整页视图互斥：Git 视图激活时先关闭
  if (gitViewActive) { gitViewActive = false; gitTreeBackup = null; }
  if (!searchViewActive) {
    searchTreeBackup = viewEl.firstChild;
    searchViewActive = true;
  }
  renderSearchView();
  renderStatusbar();
  const input = document.getElementById('sw-input');
  if (input) {
    if (prefill) input.value = prefill;
    else if (searchResults && searchResults.query) input.value = searchResults.query;
    input.focus();
    input.select();
  }
}

function closeSearchView() {
  searchViewActive = false;
  searchResults = null;
  if (activePath === TREE_TAB) {
    renderView();
    renderStatusbar();
  }
}

async function runSearch() {
  const input = document.getElementById('sw-input');
  const q = input ? input.value : '';
  if (!q.trim()) return;
  const regex = document.getElementById('sw-regex').checked;
  const ci = document.getElementById('sw-case').checked;
  const seq = ++searchSeq;
  searchLoading = true;
  renderSearchResults();
  const params = new URLSearchParams({ q, session_id: SESSION_ID });
  if (regex) params.set('regex', '1');
  if (ci) params.set('case', 'i');
  try {
    const resp = await api('/plugins/ide/search?' + params);
    const data = await resp.json();
    if (seq !== searchSeq) return;
    searchResults = data;
  } catch (e) {
    if (seq !== searchSeq) return;
    searchResults = { error: e.message, files: [] };
  }
  searchLoading = false;
  if (searchViewActive) renderSearchResults();
}

// 打开文件并定位到指定行：固定标签，加载完成后把游标移到行首并滚动。
function openMatch(path, lineno) {
  let tab = findTab(path);
  if (!tab) {
    openFileTab(path, true);
    tab = findTab(path);
  } else {
    if (!tab.pinned) { tab.pinned = true; renderTabs(); }
    activate(path);
  }
  searchJumpLine = lineno;
  searchJumpPath = path;
  if (tab && tab.loaded && tab.kind === 'text') doSearchJump();
}

let searchJumpLine = 0;
let searchJumpPath = '';

// renderEditor 重建编辑器后（或文件加载完成后）执行一次定位。
function doSearchJump() {
  if (!searchJumpLine || searchJumpPath !== activePath) return;
  const tab = findTab(activePath);
  if (!tab || !tab._view) return;
  const line = Math.min(searchJumpLine, tab._view.state.doc.lines);
  const pos = tab._view.state.doc.line(line).from;
  tab._view.dispatch({ selection: { anchor: pos }, scrollIntoView: true });
  tab._view.focus();
  searchJumpLine = 0;
}

function renderSearchView() {
  viewEl.innerHTML =
    '<div class="sw-root">'
    + '<div class="sw-bar">'
    +   '<input id="sw-input" class="sw-input" type="text" placeholder="搜索工作区内容…">'
    +   '<label class="sw-opt"><input id="sw-regex" type="checkbox">正则</label>'
    +   '<label class="sw-opt"><input id="sw-case" type="checkbox">忽略大小写</label>'
    +   '<button id="sw-go" class="sw-go" type="button">搜索</button>'
    +   '<button id="sw-close" class="sw-go" type="button">返回</button>'
    + '</div>'
    + '<div class="sw-results" id="sw-results"></div>'
    + '</div>';
  const input = document.getElementById('sw-input');
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') runSearch();
  });
  document.getElementById('sw-go').addEventListener('click', runSearch);
  document.getElementById('sw-close').addEventListener('click', closeSearchView);
  document.getElementById('sw-regex').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') runSearch();
  });
  renderSearchResults();
}

function renderSearchResults() {
  const box = document.getElementById('sw-results');
  if (!box) return;
  if (searchLoading) {
    box.innerHTML = '<div class="status">搜索中…</div>';
    return;
  }
  if (!searchResults) {
    box.innerHTML = '<div class="status">输入关键词后按 Enter 搜索</div>';
    return;
  }
  if (searchResults.error) {
    box.innerHTML = '<div class="status">搜索失败：' + esc(searchResults.error) + '</div>';
    return;
  }
  const files = searchResults.files || [];
  if (files.length === 0) {
    box.innerHTML = '<div class="status">无匹配</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  const head = document.createElement('div');
  head.className = 'sw-summary';
  head.textContent = searchResults.total + ' 处匹配，' + files.length + ' 个文件'
    + (searchResults.truncated ? '（结果过多，已截断）' : '');
  frag.appendChild(head);
  for (const f of files) {
    const group = document.createElement('div');
    group.className = 'sw-file';
    const fname = document.createElement('button');
    fname.type = 'button';
    fname.className = 'sw-fname';
    fname.textContent = f.path;
    fname.title = f.path;
    fname.addEventListener('click', () => openMatch(f.path, f.matches[0] ? f.matches[0].line : 1));
    group.appendChild(fname);
    for (const m of f.matches) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'sw-match';
      row.title = f.path + ':' + m.line;
      const ln = document.createElement('span');
      ln.className = 'sw-ln';
      ln.textContent = m.line;
      row.appendChild(ln);
      const ct = document.createElement('span');
      ct.className = 'sw-ct';
      ct.textContent = m.content;
      row.appendChild(ct);
      row.addEventListener('click', () => openMatch(f.path, m.line));
      group.appendChild(row);
    }
    frag.appendChild(group);
  }
  box.innerHTML = '';
  box.appendChild(frag);
}

/* 工作区文件树：懒加载拉取、节点渲染、首次挂载、文件操作与局部刷新。 */

// ── file tree ────────────────────────────────────────────────────────────

async function fetchTree(subPath) {
  const params = new URLSearchParams();
  if (SESSION_ID) params.set('session_id', SESSION_ID);
  if (subPath) params.set('sub_path', subPath);
  params.set('depth', '2');
  const resp = await api('/workspace/tree?' + params);
  return resp.json();
}

// ── 节点注册表：path -> record，供局部刷新与文件操作定位 ──
// record: { node, isDir, depth, btn, childBox, parentPath, loaded, expanded,
//           setExpanded(n), childPaths }
const nodeIndex = new Map();
let rootBox = null;          // 根层子节点容器（路径栏之下）

function parentDirOf(path) {
  const i = String(path).lastIndexOf('/');
  return i < 0 ? '' : String(path).slice(0, i);
}

function unregisterSubtree(rec) {
  for (const p of rec.childPaths) {
    const child = nodeIndex.get(p);
    if (child) unregisterSubtree(child);
    nodeIndex.delete(p);
  }
  rec.childPaths = [];
}

function rebuildChildren(rec, children) {
  unregisterSubtree(rec);
  rec.childBox.innerHTML = '';
  rec.childPaths = children.map((c) => c.path);
  children.forEach((c) => renderNode(c, rec.depth + 1, rec.childBox));
  if (children.length === 0) rec.childBox.innerHTML = '<div class="status">空目录</div>';
}

function renderNode(node, depth, container) {
  const isDir = node.type === 'directory';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'node';
  btn.style.paddingLeft = (depth * 14 + 10) + 'px';
  btn.title = node.path;
  const badge = node.truncated ? ' (' + (node.childDirs || 0) + 'd ' + (node.childFiles || 0) + 'f)' : '';
  btn.innerHTML =
    '<span class="tw">' + (isDir ? '▸' : '') + '</span>' +
    '<span class="nm">' + esc(node.name) + (badge ? '<span style="color:var(--tertiary)">' + esc(badge) + '</span>' : '') + '</span>' +
    (isDir ? '' : '<span class="sz">' + esc(fmtSize(node.size)) + '</span>');
  container.appendChild(btn);

  const childBox = document.createElement('div');
  childBox.style.display = 'none';
  container.appendChild(childBox);

  const rec = {
    node, isDir, depth, btn, childBox,
    parentPath: parentDirOf(node.path),
    loaded: false, expanded: false, childPaths: [],
  };
  nodeIndex.set(node.path, rec);

  rec.setExpanded = (next) => {
    rec.expanded = next;
    childBox.style.display = next ? '' : 'none';
    btn.querySelector('.tw').textContent = isDir ? (next ? '▾' : '▸') : '';
  };

  btn.addEventListener('click', async () => {
    if (!isDir) {
      treeRootEl.querySelectorAll('.node.active').forEach((el) => el.classList.remove('active'));
      btn.classList.add('active');
      openFileTab(node.path, false);
      return;
    }
    if (rec.expanded) { rec.setExpanded(false); return; }
    if (!rec.loaded) {
      rec.loaded = true;
      let children = node.children || [];
      if (node.truncated || children.length === 0) {
        try {
          const data = await fetchTree(node.path === '.' ? '' : node.path);
          children = (data.tree && data.tree.children) || [];
        } catch (e) {
          children = [];
        }
      }
      // 加载期间该节点可能已被局部刷新替换，过期结果丢弃
      if (nodeIndex.get(node.path) !== rec) return;
      rebuildChildren(rec, children);
    }
    rec.setExpanded(true);
  });

  btn.addEventListener('dblclick', () => {
    if (!isDir) openFileTab(node.path, true);
  });

  // [AutoC 2026-08-27] 右键菜单：查看操作 + 文件管理操作。
  // 编辑器区域不拦截，保留浏览器原生菜单（复制粘贴仍可用）。
  btn.addEventListener('contextmenu', (e) => {
    const items = [];
    if (isDir) {
      items.push({ label: '新建文件', action: () => opNew(node.path, true) });
      items.push({ label: '新建目录', action: () => opNew(node.path, false) });
      if (cutPath) items.push({ label: '粘贴（移动 ' + baseName(cutPath) + '）', action: () => opPaste(node.path) });
      items.push({ label: rec.expanded ? '折叠' : '展开', action: () => btn.click() });
    } else {
      items.push({ label: '打开', action: () => openFileTab(node.path, false) });
      items.push({ label: '固定打开', action: () => openFileTab(node.path, true) });
    }
    items.push({ label: '剪切（移动）', action: () => { cutPath = node.path; } });
    items.push({ label: '重命名', action: () => opRename(node) });
    items.push({ label: '删除', danger: true, action: () => opDelete(node) });
    items.push({ label: '复制相对路径', action: () => copyText(node.path) });
    items.push({ label: '复制名称', action: () => copyText(node.name) });
    e.preventDefault();
    e.stopPropagation();
    showContextMenu(e.clientX, e.clientY, items);
  });
}

// ── 局部刷新 ──────────────────────────────────────────────────────────
// 目录刷新只重建该目录的子节点容器（保留其他展开状态）；根刷新重建根层。
// 刷新后目录强制展开，让操作结果（新建/删除/移入）直接可见。

async function refreshDir(dirPath) {
  if (!dirPath || dirPath === '.') return refreshRoot();
  const rec = nodeIndex.get(dirPath);
  if (!rec || !rec.isDir) return;
  try {
    const data = await fetchTree(dirPath);
    if (nodeIndex.get(dirPath) !== rec) return;
    const children = (data.tree && data.tree.children) || [];
    rec.loaded = true;
    rebuildChildren(rec, children);
    if (!rec.expanded) rec.setExpanded(true);
  } catch (e) { /* 静默：下次展开会重试 */ }
}

async function refreshRoot() {
  try {
    const data = await fetchTree('');
    if (!rootBox) return;
    const children = (data.tree && data.tree.children) || [];
    nodeIndex.clear();
    rootBox.innerHTML = '';
    children.forEach((c) => renderNode(c, 0, rootBox));
    if (children.length === 0) rootBox.innerHTML = '<div class="status">空目录或未设置工作区</div>';
  } catch (e) { /* 静默 */ }
}

// ── 文件操作 ──────────────────────────────────────────────────────────

let cutPath = '';

function fsQuery(extra) {
  const p = new URLSearchParams();
  if (SESSION_ID) p.set('session_id', SESSION_ID);
  for (const k of Object.keys(extra || {})) p.set(k, extra[k]);
  return p.toString();
}

async function refreshParentOf(path) {
  await refreshDir(parentDirOf(path));
}

async function opDelete(node) {
  const msg = node.type === 'directory'
    ? '删除目录 ' + node.path + ' 及其全部内容？'
    : '删除文件 ' + node.path + '？';
  if (!confirm(msg)) return;
  try {
    await api('/plugins/ide/fs/delete?' + fsQuery({ path: node.path }), { method: 'POST' });
  } catch (e) {
    alert('删除失败：' + e.message);
    return;
  }
  closeTabsUnder(node.path);
  await refreshParentOf(node.path);
  void refreshGit();
}

async function opRename(node) {
  const newName = prompt('重命名「' + node.name + '」为：', node.name);
  if (!newName || newName === node.name) return;
  if (newName.includes('/')) { alert('名称不能包含 /'); return; }
  const parent = parentDirOf(node.path);
  const to = parent ? parent + '/' + newName : newName;
  try {
    await api('/plugins/ide/fs/move?' + fsQuery({ from: node.path, to }), { method: 'POST' });
  } catch (e) {
    alert('重命名失败：' + e.message);
    return;
  }
  closeTabsUnder(node.path);
  await refreshParentOf(node.path);
  void refreshGit();
}

async function opNew(dirPath, isFile) {
  const name = prompt(isFile ? '新文件名：' : '新目录名：');
  if (!name) return;
  if (name.includes('/')) { alert('名称不能包含 /'); return; }
  const full = dirPath ? dirPath + '/' + name : name;
  try {
    if (isFile) {
      await api('/plugins/ide/file?' + fsQuery({ path: full }), { method: 'PUT', body: '' });
    } else {
      await api('/plugins/ide/fs/mkdir?' + fsQuery({ path: full }), { method: 'POST' });
    }
  } catch (e) {
    alert('创建失败：' + e.message);
    return;
  }
  await refreshDir(dirPath);
  void refreshGit();
}

async function opPaste(dirPath) {
  if (!cutPath) return;
  const src = cutPath;
  const to = dirPath ? dirPath + '/' + baseName(src) : baseName(src);
  if (to === src) { cutPath = ''; return; }
  try {
    await api('/plugins/ide/fs/move?' + fsQuery({ from: src, to }), { method: 'POST' });
  } catch (e) {
    alert('移动失败：' + e.message);
    return;
  }
  cutPath = '';
  closeTabsUnder(src);
  await refreshDir(dirPath);
  const srcParent = parentDirOf(src);
  if (srcParent !== dirPath) await refreshDir(srcParent);
  void refreshGit();
}

// 根层（文件树空白处）右键：根级新建与粘贴
function showRootMenu(x, y) {
  const items = [
    { label: '新建文件', action: () => opNew('', true) },
    { label: '新建目录', action: () => opNew('', false) },
  ];
  if (cutPath) items.push({ label: '粘贴（移动 ' + baseName(cutPath) + '）', action: () => opPaste('') });
  showContextMenu(x, y, items);
}

// ── 首次挂载 ──────────────────────────────────────────────────────────

async function bootTree() {
  if (!SESSION_ID) {
    if (activePath === TREE_TAB) viewEl.innerHTML = '<div class="status">缺少会话上下文</div>';
    return;
  }
  if (activePath === TREE_TAB) viewEl.innerHTML = '<div class="status">加载中…</div>';
  try {
    const data = await fetchTree('');
    treeRootEl = document.createElement('div');
    treeRootEl.className = 'tree-root';
    if (data.workspace_path) {
      const p = document.createElement('div');
      p.className = 'tree-path';
      const ws = document.createElement('span');
      ws.className = 'ws';
      ws.textContent = data.workspace_path;
      ws.title = data.workspace_path;
      p.appendChild(ws);
      p.appendChild(buildGitButton());
      treeRootEl.appendChild(p);
    }
    rootBox = document.createElement('div');
    treeRootEl.appendChild(rootBox);
    nodeIndex.clear();
    const children = (data.tree && data.tree.children) || [];
    children.forEach((c) => renderNode(c, 0, rootBox));
    if (children.length === 0) rootBox.innerHTML = '<div class="status">空目录或未设置工作区</div>';
    // 根背景右键：节点与路径栏之外的区域提供根层操作
    treeRootEl.addEventListener('contextmenu', (e) => {
      if (e.target.closest('.node') || e.target.closest('.tree-path')) return;
      e.preventDefault();
      showRootMenu(e.clientX, e.clientY);
    });
    treeBooted = true;
    // [AutoC 2026-08-26] 竞态修复：bootTree 与 loadFileTab 并发时，树请求
    // 后完成会无条件把树塞进 viewEl，覆盖已激活的文件标签视图（首次点击
    // “在 IDE 打开”只出标签不跳转的根因）。只在当前仍是文件树标签时才渲染，
    // 否则只缓存树数据，切回文件树时由 renderView 正常挂载。
    if (activePath === TREE_TAB) {
      viewEl.innerHTML = '';
      viewEl.appendChild(treeRootEl);
    }
  } catch (err) {
    if (activePath === TREE_TAB) viewEl.innerHTML = '<div class="status">加载失败：' + esc(err.message) + '</div>';
  }
}

// ── file loading & classification ────────────────────────────────────────

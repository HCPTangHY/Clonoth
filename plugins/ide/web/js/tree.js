/* 工作区文件树：懒加载拉取、节点渲染、首次挂载。 */

// ── file tree ────────────────────────────────────────────────────────────

async function fetchTree(subPath) {
  const params = new URLSearchParams();
  if (SESSION_ID) params.set('session_id', SESSION_ID);
  if (subPath) params.set('sub_path', subPath);
  params.set('depth', '2');
  const resp = await api('/workspace/tree?' + params);
  return resp.json();
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

  let loaded = false;
  let expanded = false;

  function setExpanded(next) {
    expanded = next;
    childBox.style.display = expanded ? '' : 'none';
    btn.querySelector('.tw').textContent = isDir ? (expanded ? '▾' : '▸') : '';
  }

  btn.addEventListener('click', async () => {
    if (!isDir) {
      treeRootEl.querySelectorAll('.node.active').forEach((el) => el.classList.remove('active'));
      btn.classList.add('active');
      openFileTab(node.path, false);
      return;
    }
    if (expanded) { setExpanded(false); return; }
    if (!loaded) {
      loaded = true;
      let children = node.children || [];
      if (node.truncated || children.length === 0) {
        try {
          const data = await fetchTree(node.path === '.' ? '' : node.path);
          children = (data.tree && data.tree.children) || [];
        } catch (e) {
          children = [];
        }
      }
      childBox.innerHTML = '';
      children.forEach((c) => renderNode(c, depth + 1, childBox));
      if (children.length === 0) {
        childBox.innerHTML = '<div class="status">空目录</div>';
      }
    }
    setExpanded(true);
  });

  btn.addEventListener('dblclick', () => {
    if (!isDir) openFileTab(node.path, true);
  });
}

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
    const children = (data.tree && data.tree.children) || [];
    if (children.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'status';
      empty.textContent = '空目录或未设置工作区';
      treeRootEl.appendChild(empty);
    } else {
      children.forEach((c) => renderNode(c, 0, treeRootEl));
    }
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

/* 视图调度：标签到渲染函数的分发、文件/diff 异步加载、图片渲染。 */

// ── view rendering ───────────────────────────────────────────────────────

let loadSeq = 0;
let treeBooted = false;   // 文件树只初始化一次，切换标签时保留展开状态
let treeRootEl = document.createElement('div');

function showStatus(text) {
  viewEl.innerHTML = '<div class="status">' + esc(text) + '</div>';
}

function stashScroll() {
  const tab = viewPath && viewPath !== TREE_TAB ? findTab(viewPath) : null;
  if (!tab) return;
  if (tab.kind === 'text') {
    if (tab._view) {
      tab.scroll = scrollFraction(tab._view.scrollDOM);
      tab.cursor = tab._view.state.selection.main.head;
    } else if (isMarkdown(tab.path) && tab.mode === 'render') {
      tab.scroll = scrollFraction(viewEl);
    }
  } else if ((tab.kind === 'diff' || tab.kind === 'gitdiff') && tab._view) {
    // diff 视图现在是 CodeMirror，滚动容器从 viewEl 换为编辑器 scroller。
    tab.scroll = scrollFraction(tab._view.scrollDOM);
  }
}

function renderView() {
  stashScroll();
  viewPath = activePath;
  if (activePath === TREE_TAB) {
    // Git 整页视图激活时替换文件树显示
    if (gitViewActive) { renderGitView(); renderStatusbar(); return; }
    if (treeBooted) {
      viewEl.innerHTML = '';
      viewEl.appendChild(treeRootEl);
    } else {
      bootTree();
    }
    renderStatusbar();
    return;
  }
  const tab = findTab(activePath);
  if (!tab) { renderStatusbar(); return; }
    if (!tab.loaded) {
    // gitdiff 的数据由 openGitDiffTab 主动加载，renderView 不触发
    if (tab.kind === 'gitdiff') { renderStatusbar(); return; }
    if (tab.kind === 'diff') { loadDiffTab(tab); return; }
    loadFileTab(tab);
    return;
  }
  renderFileTab(tab);
  renderStatusbar();
}

function renderFileTab(tab) {
  if (tab.kind === 'diff') renderDiff(tab);
  else if (tab.kind === 'gitdiff') renderDiff(tab);
  else if (tab.kind === 'text') {
    if (isMarkdown(tab.path) && tab.mode === 'render') renderMarkdown(tab);
    else renderEditor(tab);
  }
  else if (tab.kind === 'image') renderImage(tab);
  else showStatus(tab.note || '不支持预览');
}

async function loadFileTab(tab) {
  const seq = ++loadSeq;
  showStatus('加载中…');
  renderStatusbar();
  let resp;
  try {
    resp = await api('/sessions/' + encodeURIComponent(SESSION_ID)
      + '/file?path=' + encodeURIComponent(tab.path));
  } catch (err) {
    if (activePath === tab.path) showStatus('加载失败：' + err.message);
    return;
  }
  const ct = resp.headers.get('content-type') || '';
  const ext = extOf(tab.path);
  if (ct.startsWith('image/') || IMAGE_EXTS.has(ext)) {
    const blob = await resp.blob();
    tab.kind = 'image';
    tab.blobUrl = URL.createObjectURL(blob);
  } else {
    const buf = await resp.arrayBuffer();
    if (buf.byteLength > MAX_TEXT_BYTES * 4) {
      tab.kind = 'binary';
      tab.note = '二进制文件（' + buf.byteLength + ' B），不支持预览';
    } else {
      const bytes = new Uint8Array(buf);
      let isBinary = false;
      for (let i = 0; i < Math.min(bytes.length, 8192); i++) {
        if (bytes[i] === 0) { isBinary = true; break; }
      }
      if (isBinary) {
        tab.kind = 'binary';
        tab.note = '二进制文件（' + buf.byteLength + ' B），不支持预览';
      } else {
        let text = new TextDecoder('utf-8').decode(bytes);
        if (text.length > MAX_TEXT_BYTES) {
          text = text.slice(0, MAX_TEXT_BYTES);
          tab.note = '文件过大，仅加载前 ' + MAX_TEXT_BYTES + ' 字节，保存将被拒绝';
          tab.kind = 'binary';   // 截断内容不可写回，按只读处理
        } else {
          tab.kind = 'text';
          tab.text = text;
          tab.savedText = text;
        }
      }
    }
  }
  tab.loaded = true;
  // 仅当本次加载仍是最新且该标签仍激活时才渲染；过期的响应直接丢弃，
  // 内容已缓存进 tab，切回时直接命中。
  if (activePath === tab.path && seq === loadSeq) {
    renderFileTab(tab);
    renderStatusbar();
  }
}

async function loadDiffTab(tab) {
  const seq = ++loadSeq;
  showStatus('加载中…');
  renderStatusbar();
  // diff 标签的 path 带 ' (diff)' 后缀，取原始路径读文件
  const filePath = tab.path.replace(/ \(diff\)$/, '');
  let resp;
  try {
    resp = await api('/sessions/' + encodeURIComponent(SESSION_ID)
      + '/file?path=' + encodeURIComponent(filePath));
  } catch (err) {
    if (activePath === tab.path) showStatus('加载失败：' + err.message);
    return;
  }
  const current = await resp.text();
  // 工具执行完后文件已是 after 态。逆应用 diffs（replace → search，倒序）
  // 得到 before 态。逆应用失败的条目说明文件状态与 diff 不匹配，此时
  // before = current（该条 diff 不体现在对比中）。
  let before = current;
  let unmatched = 0;
  const revDiffs = (tab.diffs || []).slice().reverse();
  for (const d of revDiffs) {
    const search = String(d.search || '');
    const replace = String(d.replace || '');
    if (replace && before.includes(replace)) {
      before = before.replace(replace, search);
    } else if (replace) {
      unmatched++;
    }
  }
  tab.text = before;        // 修改前
  tab.afterText = current;  // 修改后（当前文件）
  tab.loaded = true;
  if (unmatched > 0) tab.note = unmatched + ' 处 diff 无法逆向匹配';
  if (activePath === tab.path && seq === loadSeq) {
    renderFileTab(tab);
    renderStatusbar();
  }
}

function renderImage(tab) {
  viewEl.innerHTML = '';
  const img = document.createElement('img');
  img.className = 'pv-img';
  img.src = tab.blobUrl;
  img.alt = tab.path;
  viewEl.appendChild(img);
}

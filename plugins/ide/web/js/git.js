/* Git：入口按钮与徽章、整页源代码管理视图（提交/暂存/时间轴/提交详情）。 */

// ── git ─────────────────────────────────────────────────────────────────
// 入口是文件树路径栏右侧的分支按钮（buildGitButton，bootTree 时挂载）。
// 点击展开下拉浮层：分支名 + 刷新按钮 + 变更文件列表。列表项点击经
// openDiffTab 打开该文件的 unified diff 对比标签（kind:'gitdiff'，复用
// renderDiff 的红绿对比渲染）。非 git 仓库时徽章不显示，浮层内显示提示。

const GIT_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="6" r="3"/><path d="M6 9v6"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>';
// 状态码与操作按钮的 SVG 图标（12×12 显示，stroke 继承 currentColor）
const _SVG_OPEN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">';
const STATUS_ICONS = {
  M: _SVG_OPEN + '<path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>',
  A: _SVG_OPEN + '<path d="M12 5v14"/><path d="M5 12h14"/></svg>',
  D: _SVG_OPEN + '<path d="M5 12h14"/></svg>',
  R: _SVG_OPEN + '<path d="M17 8l4 4-4 4"/><path d="M3 12h18"/></svg>',
  '?': _SVG_OPEN + '<circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3"/><path d="M12 17h.01"/></svg>',
};
const ICON_PLUS = _SVG_OPEN + '<path d="M12 5v14"/><path d="M5 12h14"/></svg>';
const ICON_MINUS = _SVG_OPEN + '<path d="M5 12h14"/></svg>';
const ICON_REFRESH = _SVG_OPEN + '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>';
const ICON_COMMIT = _SVG_OPEN + '<circle cx="12" cy="12" r="3"/><path d="M12 2v7"/><path d="M12 15v7"/></svg>';
const ICON_REMOTE = _SVG_OPEN + '<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>';
const ICON_FORK = _SVG_OPEN + '<circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><circle cx="12" cy="18" r="3"/><path d="M6 9v3a3 3 0 0 0 3 3h3"/><path d="M18 9v3a3 3 0 0 1-3 3h-3"/></svg>';

let gitState = { is_repo: false, branch: '', changes: [], ahead: 0, behind: 0, unpushed: [] };
let gitRemote = null;   // {remote, upstream, fork_point}，openGitView 时加载
let gitPopEl = null;
let gitBadgeEl = null;
// 整页 Git 视图的激活状态：gitViewActive 为 true 时 renderView 渲染 Git 页
// 而不是文件树/文件内容。gitTreeBackup 暂存文件树 DOM，返回时恢复。
let gitViewActive = false;
let gitTreeBackup = null;
let gitLogCache = null;
let gitCommitMsg = '';
// 展开的提交 hash 与其详情数据（点击提交行展开/收起）
let expandedCommitHash = '';
let expandedCommitData = null;

async function fetchGitStatus() {
  const resp = await api('/plugins/ide/git/status?session_id=' + encodeURIComponent(SESSION_ID));
  return resp.json();
}

async function refreshGit() {
  try {
    gitState = await fetchGitStatus();
  } catch {
    gitState = { is_repo: false, branch: '', changes: [], ahead: 0, behind: 0, unpushed: [] };
  }
  updateGitBadge();
  if (gitPopEl) renderGitPop();
  if (gitViewActive) renderGitView();
}

function updateGitBadge() {
  if (!gitBadgeEl) return;
  // 工作区变更 + 未推送提交都是用户关心的“变更”
  const n = gitState.is_repo ? gitState.changes.length + (gitState.ahead || 0) : 0;
  if (n > 0) {
    gitBadgeEl.textContent = n > 99 ? '99+' : String(n);
    gitBadgeEl.style.display = '';
  } else {
    gitBadgeEl.style.display = 'none';
  }
}

function buildGitButton() {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'git-btn';
  btn.title = '源代码管理';
  btn.innerHTML = GIT_ICON + '<span class="git-badge" style="display:none"></span>';
  gitBadgeEl = btn.querySelector('.git-badge');
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    // 整页 Git 视图：替换文件树显示，右上角返回
    openGitView();
  });
  // 首次构造时拉一次 status 填徽章
  void refreshGit();
  return btn;
}

function openGitPop(anchorBtn) {
  closeGitPop();
  const pop = document.createElement('div');
  pop.className = 'git-pop';
  gitPopEl = pop;
  anchorBtn.parentElement.appendChild(pop);
  renderGitPop();
  const onDocDown = (ev) => {
    if (!gitPopEl) return;
    if (gitPopEl.contains(ev.target) || anchorBtn.contains(ev.target)) return;
    closeGitPop();
  };
  // 延迟一帧挂监听，避免本次点击立即触发关闭
  setTimeout(() => document.addEventListener('pointerdown', onDocDown), 0);
  pop._onDocDown = onDocDown;
}

function closeGitPop() {
  if (!gitPopEl) return;
  if (gitPopEl._onDocDown) document.removeEventListener('pointerdown', gitPopEl._onDocDown);
  gitPopEl.remove();
  gitPopEl = null;
}

function renderGitPop() {
  const pop = gitPopEl;
  if (!pop) return;
  pop.innerHTML = '';
  const head = document.createElement('div');
  head.className = 'git-pop-head';
  const branch = document.createElement('span');
  branch.className = 'branch';
  branch.textContent = gitState.is_repo ? gitState.branch : '源代码管理';
  head.appendChild(branch);
  const refresh = document.createElement('button');
  refresh.type = 'button';
  refresh.title = '刷新';
  refresh.textContent = '↻';
  refresh.addEventListener('click', (e) => { e.stopPropagation(); void refreshGit(); });
  head.appendChild(refresh);
  pop.appendChild(head);

  const body = document.createElement('div');
  body.className = 'git-pop-body';
  if (!gitState.is_repo) {
    body.innerHTML = '<div class="git-pop-empty">当前工作区不是 git 仓库</div>';
  } else if (gitState.changes.length === 0) {
    body.innerHTML = '<div class="git-pop-empty">没有变更</div>';
  } else {
    for (const ch of gitState.changes) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'git-row';
      row.title = ch.path;
      const st = document.createElement('span');
      st.className = 'st ' + ch.status + (ch.staged ? ' staged' : '');
      st.innerHTML = STATUS_ICONS[ch.status] || ch.status;
      st.title = ch.staged ? '已暂存' : '未暂存';
      row.appendChild(st);
      const p = document.createElement('span');
      p.className = 'p';
      p.textContent = ch.path;
      row.appendChild(p);
      row.addEventListener('click', () => {
        closeGitPop();
        void openGitDiffTab(ch.path);
      });
      body.appendChild(row);
    }
  }
  pop.appendChild(body);
}

async function openGitDiffTab(relPath) {
  const key = relPath + ' (diff)';
  let tab = findTab(key);
  if (!tab) {
    tab = newDiffTab(key, []);
    tab.kind = 'gitdiff';
    tabs.push(tab);
  }
  activePath = key;
  renderTabs();
  renderView();
  // 加载 unified diff 并解析为 renderDiff 的 {search, replace} 结构
  tab.loaded = false;
  let diffText = '';
  try {
    const resp = await api('/plugins/ide/git/diff?session_id=' + encodeURIComponent(SESSION_ID)
      + '&path=' + encodeURIComponent(relPath));
    const data = await resp.json();
    diffText = data.diff || '';
  } catch (err) {
    tab.note = '加载 diff 失败：' + err.message;
    tab.kind = 'binary';
    tab.loaded = true;
    if (activePath === key) { renderFileTab(tab); renderStatusbar(); }
    return;
  }
  const parsed = parseUnifiedDiff(diffText);
  tab.text = parsed.before;
  tab.afterText = parsed.after;
  tab.loaded = true;
  if (activePath === key) { renderFileTab(tab); renderStatusbar(); }
}

// 解析 unified diff 为整块 before/after 文本，复用 renderDiff 的行级对比。
// 只处理常规 @@ 块；多文件 diff 只取第一个文件（git diff -- <path> 只会有一个）。
function parseUnifiedDiff(text) {
  const before = [];
  const after = [];
  let inHunk = false;
  for (const line of text.split('\n')) {
    if (line.startsWith('@@')) { inHunk = true; continue; }
    if (!inHunk) continue;
    if (line.startsWith('diff --git')) break;
    if (line.startsWith('---') || line.startsWith('+++')) continue;
    if (line.startsWith('\\')) continue; // "No newline at end of file"
    if (line.startsWith('-')) { before.push(line.slice(1)); continue; }
    if (line.startsWith('+')) { after.push(line.slice(1)); continue; }
    // 上下文行同时进两侧
    const body = line.startsWith(' ') ? line.slice(1) : line;
    before.push(body);
    after.push(body);
  }
  return { before: before.join('\n'), after: after.join('\n') };
}

// ── 整页 Git 视图 ─────────────────────────────────────────────────────
// 点击文件树 git 按钮后，视图区整体切换为 Git 页（提交区 + 变更列表 +
// 提交记录时间轴），右上角返回文件树。文件树 DOM 由 gitTreeBackup 暂存，
// 返回时原样恢复，树的展开状态不丢。

async function openGitView() {
  if (activePath !== TREE_TAB) {
    // 从文件标签进入：先切回文件树标签，再替换视图
    activePath = TREE_TAB;
    renderTabs();
  }
  if (!gitViewActive) {
    gitTreeBackup = viewEl.firstChild;
    gitViewActive = true;
  }
  closeGitPop();
  // 并行拉 status、log 和远程信息
  void refreshGit();
  void loadGitLog();
  void loadGitRemote();
  renderGitView();
  renderStatusbar();
}

async function loadGitRemote() {
  try {
    const resp = await api('/plugins/ide/git/remote?session_id=' + encodeURIComponent(SESSION_ID));
    gitRemote = await resp.json();
  } catch {
    gitRemote = null;
  }
  if (gitViewActive) renderGitView();
}

function closeGitView() {
  gitViewActive = false;
  gitLogCache = null;
  if (activePath === TREE_TAB) {
    renderView();
    renderStatusbar();
  }
}

async function loadGitLog() {
  try {
    const resp = await api('/plugins/ide/git/log?session_id=' + encodeURIComponent(SESSION_ID));
    gitLogCache = await resp.json();
  } catch {
    gitLogCache = { is_repo: false, commits: [] };
  }
  if (gitViewActive) renderGitView();
}

function relTime(ts) {
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
  if (diff < 86400 * 30) return Math.floor(diff / 86400) + ' 天前';
  const d = new Date(ts * 1000);
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}

function renderGitView() {
  viewEl.innerHTML = '';
  const page = document.createElement('div');
  page.className = 'git-page';

  // ── 顶行：返回 + 分支名 + 刷新 ──
  const top = document.createElement('div');
  top.className = 'git-page-top';
  const back = document.createElement('button');
  back.type = 'button';
  back.className = 'git-back';
  back.textContent = '← 返回';
  back.addEventListener('click', closeGitView);
  top.appendChild(back);
  const branch = document.createElement('span');
  branch.className = 'git-page-branch';
  branch.textContent = gitState.is_repo
    ? gitState.branch + (gitState.ahead > 0 ? ' ↑' + gitState.ahead : '') + (gitState.behind > 0 ? ' ↓' + gitState.behind : '')
    : '';
  top.appendChild(branch);
  const refresh = document.createElement('button');
  refresh.type = 'button';
  refresh.className = 'git-iconbtn';
  refresh.title = '刷新';
  refresh.innerHTML = ICON_REFRESH;
  refresh.addEventListener('click', () => { void refreshGit(); void loadGitLog(); });
  top.appendChild(refresh);
  page.appendChild(top);

  // ── 远程仓库信息行（origin / upstream / fork 点）──
  if (gitState.is_repo && gitRemote) {
    const rl = document.createElement('div');
    rl.className = 'git-remote-line';
    let html = '<span class="git-ic">' + ICON_REMOTE + '</span>';
    if (gitRemote.remote) html += '<span class="git-remote-url">' + esc(gitRemote.remote) + '</span>';
    if (gitRemote.upstream) html += '<span class="git-remote-up">跟踪 ' + esc(gitRemote.upstream) + '</span>';
    if (gitRemote.fork_point) {
      html += '<span class="git-ic">' + ICON_FORK + '</span>'
        + '<code class="git-fork-hash">' + esc(gitRemote.fork_point.hash) + '</code>'
        + '<span class="git-fork-subj">' + esc(gitRemote.fork_point.subject) + '</span>';
    }
    rl.innerHTML = html;
    page.appendChild(rl);
  }

  if (!gitState.is_repo) {
    const empty = document.createElement('div');
    empty.className = 'status';
    empty.textContent = '当前工作区不是 git 仓库';
    page.appendChild(empty);
    viewEl.appendChild(page);
    return;
  }

  // ── 提交区 ──
  const commitBox = document.createElement('div');
  commitBox.className = 'git-commit';
  const ta = document.createElement('textarea');
  ta.className = 'git-commit-msg';
  ta.placeholder = '提交信息';
  ta.value = gitCommitMsg;
  ta.rows = 2;
  ta.addEventListener('input', () => { gitCommitMsg = ta.value; });
  commitBox.appendChild(ta);
  const commitBtn = document.createElement('button');
  commitBtn.type = 'button';
  commitBtn.className = 'git-commit-btn';
  const stagedCount = gitState.changes.filter(c => c.staged).length;
  commitBtn.textContent = stagedCount > 0 ? '提交（' + stagedCount + ' 项已暂存）' : '提交';
  commitBtn.disabled = stagedCount === 0;
  commitBtn.addEventListener('click', async () => {
    const msg = gitCommitMsg.trim();
    if (!msg) { ta.focus(); return; }
    commitBtn.disabled = true;
    commitBtn.textContent = '提交中…';
    try {
      const resp = await api('/plugins/ide/git/commit?session_id=' + encodeURIComponent(SESSION_ID), {
        method: 'POST',
        headers: { 'Content-Type': 'text/plain; charset=utf-8' },
        body: msg,
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '提交失败');
      gitCommitMsg = '';
      void refreshGit();
      void loadGitLog();
    } catch (err) {
      alert('提交失败：' + err.message);
      commitBtn.disabled = false;
      commitBtn.textContent = '提交';
    }
  });
  commitBox.appendChild(commitBtn);
  page.appendChild(commitBox);

  // ── 变更列表：暂存的变更 / 变更 两个分组 ──
  const staged = gitState.changes.filter(c => c.staged);
  const unstaged = gitState.changes.filter(c => !c.staged);
  const mkGroup = (title, items, actionLabel, actionFn) => {
    if (!items.length) return;
    const g = document.createElement('div');
    g.className = 'git-group';
    const gh = document.createElement('div');
    gh.className = 'git-group-head';
    gh.textContent = title + '（' + items.length + '）';
    g.appendChild(gh);
    for (const ch of items) {
      const row = document.createElement('div');
      row.className = 'git-page-row';
      const st = document.createElement('span');
      st.className = 'st ' + ch.status;
      st.innerHTML = STATUS_ICONS[ch.status] || ch.status;
      row.appendChild(st);
      const p = document.createElement('span');
      p.className = 'p';
      p.textContent = ch.path;
      p.title = ch.path;
      row.appendChild(p);
      const act = document.createElement('button');
      act.type = 'button';
      act.className = 'git-row-act';
      act.innerHTML = actionLabel === '+' ? ICON_PLUS : ICON_MINUS;
      act.title = actionLabel === '+' ? '暂存' : '取消暂存';
      act.addEventListener('click', async (e) => {
        e.stopPropagation();
        try {
          await api('/plugins/ide/git/' + actionFn + '?session_id=' + encodeURIComponent(SESSION_ID)
            + '&path=' + encodeURIComponent(ch.path), { method: 'POST' });
          void refreshGit();
        } catch (err) {
          alert('操作失败：' + err.message);
        }
      });
      row.appendChild(act);
      p.addEventListener('click', () => { void openGitDiffTab(ch.path); });
      p.style.cursor = 'pointer';
      g.appendChild(row);
    }
    page.appendChild(g);
  };
  mkGroup('暂存的变更', staged, '−', 'unstage');
  mkGroup('变更', unstaged, '+', 'stage');
  if (!staged.length && !unstaged.length) {
    const none = document.createElement('div');
    none.className = 'git-pop-empty';
    none.textContent = '没有变更';
    page.appendChild(none);
  }

  // ── 未推送的提交（与远程的差异）──
  if (gitState.ahead > 0 && gitState.unpushed && gitState.unpushed.length) {
    const upGroup = document.createElement('div');
    upGroup.className = 'git-group';
    const uh = document.createElement('div');
    uh.className = 'git-group-head';
    uh.textContent = '未推送的提交（' + gitState.ahead + '）';
    upGroup.appendChild(uh);
    for (const c of gitState.unpushed) {
      upGroup.appendChild(buildCommitRow(c));
    }
    if (gitState.ahead > gitState.unpushed.length) {
      const more = document.createElement('div');
      more.className = 'git-pop-empty';
      more.textContent = '…另有 ' + (gitState.ahead - gitState.unpushed.length) + ' 条未显示';
      upGroup.appendChild(more);
    }
    page.appendChild(upGroup);
  }

  // ── 提交记录时间轴 ──
  if (gitLogCache && gitLogCache.is_repo && gitLogCache.commits.length) {
    const logGroup = document.createElement('div');
    logGroup.className = 'git-group';
    const lh = document.createElement('div');
    lh.className = 'git-group-head';
    lh.textContent = '提交记录';
    logGroup.appendChild(lh);
    for (const c of gitLogCache.commits) {
      logGroup.appendChild(buildCommitRow(c));
    }
    page.appendChild(logGroup);
  }

  viewEl.appendChild(page);
}

// 构建一行提交记录（含点击展开详情）。展开时行下方插入详情区：
// 完整 hash、作者、时间、完整消息、变更文件列表（点击打开 commit diff）。
function buildCommitRow(c) {
  const wrap = document.createElement('div');
  wrap.className = 'git-commit-wrap';
  const row = document.createElement('div');
  row.className = 'git-log-row' + (expandedCommitHash === c.hash ? ' expanded' : '');
  row.innerHTML = '<code class="git-log-hash">' + esc(c.hash) + '</code>'
    + '<span class="git-log-msg">' + esc(c.subject) + '</span>'
    + '<span class="git-log-time">' + esc(relTime(c.time)) + '</span>';
  row.title = c.author + ' · ' + new Date(c.time * 1000).toLocaleString() + '\n点击查看详情';
  row.addEventListener('click', () => {
    if (expandedCommitHash === c.hash) {
      expandedCommitHash = '';
      expandedCommitData = null;
      renderGitView();
    } else {
      expandedCommitHash = c.hash;
      expandedCommitData = null;
      renderGitView();
      void loadCommitDetail(c.hash);
    }
  });
  wrap.appendChild(row);
  if (expandedCommitHash === c.hash) {
    const detail = document.createElement('div');
    detail.className = 'git-commit-detail';
    if (!expandedCommitData) {
      detail.innerHTML = '<div class="git-pop-empty">加载中…</div>';
    } else {
      detail.appendChild(buildCommitDetail(expandedCommitData));
    }
    wrap.appendChild(detail);
  }
  return wrap;
}

async function loadCommitDetail(hash) {
  try {
    const resp = await api('/plugins/ide/git/show?session_id=' + encodeURIComponent(SESSION_ID)
      + '&hash=' + encodeURIComponent(hash));
    const data = await resp.json();
    if (expandedCommitHash === hash) {
      expandedCommitData = data;
      renderGitView();
    }
  } catch {
    if (expandedCommitHash === hash) {
      expandedCommitData = { error: true };
      renderGitView();
    }
  }
}

function buildCommitDetail(data) {
  const box = document.createElement('div');
  if (data.error) {
    box.innerHTML = '<div class="git-pop-empty">加载失败</div>';
    return box;
  }
  const meta = document.createElement('div');
  meta.className = 'git-detail-meta';
  meta.innerHTML = '<div><code>' + esc(data.hash) + '</code></div>'
    + '<div>' + esc(data.author) + ' &lt;' + esc(data.email) + '&gt;</div>'
    + '<div>' + new Date(data.time * 1000).toLocaleString() + '</div>';
  box.appendChild(meta);
  if (data.message) {
    const msg = document.createElement('pre');
    msg.className = 'git-detail-msg';
    msg.textContent = data.message;
    box.appendChild(msg);
  }
  if (data.files && data.files.length) {
    const fh = document.createElement('div');
    fh.className = 'git-group-head';
    fh.textContent = '变更文件（' + data.files.length + '）';
    box.appendChild(fh);
    for (const f of data.files) {
      const fr = document.createElement('button');
      fr.type = 'button';
      fr.className = 'git-detail-file';
      fr.innerHTML = '<span class="p">' + esc(f.path) + '</span>'
        + '<span class="git-fstat"><span class="add">+' + f.added + '</span> <span class="del">−' + f.deleted + '</span></span>';
      fr.title = '打开该文件在此提交的差异';
      fr.addEventListener('click', () => {
        void openCommitDiffTab(data.hash, f.path);
      });
      box.appendChild(fr);
    }
  }
  return box;
}

// 打开“提交与父提交的单文件 diff”标签，复用 renderDiff 渲染。
async function openCommitDiffTab(commitHash, relPath) {
  const key = relPath + ' @' + commitHash.slice(0, 7) + ' (diff)';
  let tab = findTab(key);
  if (!tab) {
    tab = newDiffTab(key, []);
    tab.kind = 'gitdiff';
    tabs.push(tab);
  }
  activePath = key;
  gitViewActive = false;
  renderTabs();
  renderView();
  tab.loaded = false;
  try {
    const resp = await api('/plugins/ide/git/commit_diff?session_id=' + encodeURIComponent(SESSION_ID)
      + '&hash=' + encodeURIComponent(commitHash)
      + '&path=' + encodeURIComponent(relPath));
    const data = await resp.json();
    const parsed = parseUnifiedDiff(data.diff || '');
    tab.text = parsed.before;
    tab.afterText = parsed.after;
    tab.note = '提交 ' + commitHash.slice(0, 7) + ' 的差异（与父提交对比）';
  } catch (err) {
    tab.note = '加载 diff 失败：' + err.message;
    tab.kind = 'binary';
  }
  tab.loaded = true;
  if (activePath === key) { renderFileTab(tab); renderStatusbar(); }
}

// ide.completer — 输入框 @ 文件补全（input_above 槽位）。
// 数据流：监听宿主锚定的 textarea（data-composer-textarea）原生 input 事件，
// 检测光标前未闭合的 @token，过滤 /v1/workspace/tree 扁平化文件列表，
// 渲染下拉；键盘导航走 document 级 capture（仅下拉打开时拦截）；
// 选中后先选中 token 区间，再经宿主 insertComposerText action 写回路径。
// 无宿主 DOM 结构假设之外的依赖；session 切换时重取文件树。

const MAX_ITEMS = 12;

export default {
  mount(ctx) {
    const S = ctx.state;
    S.open = false;
    S.items = [];
    S.hl = 0;
    S.filter = '';
    S.tokenStart = -1;
    S.tokenEnd = -1;
    S.files = null;          // flat path list, per sessionId
    S.treeSession = '';
    this._ctx = ctx;

    this._ta = document.querySelector('[data-composer-textarea]');
    if (!this._ta) return;

    this._onInput = () => this._detect();
    this._ta.addEventListener('input', this._onInput);

    // capture 阶段先于 React 合成事件；仅下拉打开时拦截导航键。
    this._onKey = (e) => {
      if (!S.open) return;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        e.stopPropagation();
        const d = e.key === 'ArrowDown' ? 1 : -1;
        S.hl = Math.min(Math.max(S.hl + d, 0), S.items.length - 1);
        this._render();
      } else if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        e.stopPropagation();
        const item = S.items[S.hl];
        if (item) void this._pick(item);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        this._close();
      }
    };
    document.addEventListener('keydown', this._onKey, true);

    // 点击下拉项时阻止 textarea 失焦（mousedown 先于 blur）。
    this._ui = document.createElement('div');
    this._ui.className = 'ide-completer';
    this._ui.style.display = 'none';
    this._ui.addEventListener('mousedown', (e) => e.preventDefault());
    ctx.el.appendChild(this._ui);

    this._onDocDown = (e) => {
      if (!S.open) return;
      if (ctx.el.contains(e.target) || e.target === this._ta) return;
      this._close();
    };
    document.addEventListener('mousedown', this._onDocDown);
  },

  update(ctx) {
    const prevSession = this._ctx?.data?.sessionId;
    this._ctx = ctx;
    const nextSession = ctx.data?.sessionId;
    if (prevSession !== nextSession) {
      // 会话切换：文件树属于工作区，重取。
      ctx.state.files = null;
      ctx.state.treeSession = '';
      this._close();
    }
  },

  destroy() {
    if (this._ta) {
      this._ta.removeEventListener('input', this._onInput);
      this._ta = null;
    }
    document.removeEventListener('keydown', this._onKey, true);
    document.removeEventListener('mousedown', this._onDocDown);
    if (this._ui) {
      this._ui.remove();
      this._ui = null;
    }
    this._ctx = null;
  },

  async _ensureTree() {
    const S = this._ctx.state;
    const sid = String(this._ctx.data?.sessionId || '');
    if (S.files && S.treeSession === sid) return;
    try {
      const q = sid ? `session_id=${encodeURIComponent(sid)}&` : '';
      const resp = await this._ctx.api.request(`/workspace/tree?${q}depth=8`);
      const flat = [];
      const walk = (n) => {
        if (!n || !Array.isArray(n.children)) return;
        for (const c of n.children) {
          if (c.type === 'file') flat.push(String(c.path || ''));
          else if (c.type === 'directory') walk(c);
        }
      };
      // 响应结构是 { tree: <node> }，不是裸根节点。error 节点无 children，walk 自行跳过。
      walk(resp && resp.tree);
      S.files = flat;
      S.treeSession = sid;
    } catch {
      if (!S.files) S.files = [];
    }
  },

  _detect() {
    if (!this._ta) return;
    const S = this._ctx.state;
    const v = this._ta.value;
    const cur = this._ta.selectionStart ?? v.length;
    // 从光标向前扫到第一个空白或 @，得到候选 token。
    let i = cur - 1;
    while (i >= 0 && !/[\s@]/.test(v[i])) i -= 1;
    if (i >= 0 && v[i] === '@' && (i === 0 || /\s/.test(v[i - 1]))) {
      const filter = v.slice(i + 1, cur);
      S.tokenStart = i;
      S.tokenEnd = cur;
      if (!S.open) {
        S.open = true;
        S.hl = 0;
      }
      S.filter = filter;
      void this._ensureTree().then(() => {
        this._refresh();
        this._render();
      });
      this._refresh();
      this._render();
    } else {
      this._close();
    }
  },

  _refresh() {
    const S = this._ctx.state;
    if (!S.open) return;
    const f = S.filter.toLowerCase();
    const files = S.files || [];
    const items = (f
      ? files.filter((p) => p.toLowerCase().includes(f))
      : files
    ).slice(0, MAX_ITEMS);
    S.items = items;
    if (S.hl >= items.length) S.hl = Math.max(0, items.length - 1);
  },

  async _pick(path) {
    const S = this._ctx.state;
    if (!this._ta) return;
    // 选中 @ 之后的过滤文本区间（不含 @ 本身），insertComposerText 以
    // 选区替换语义写入完整路径。@ 必须保留在草稿里：落库与渲染保持
    // @path 字面量，engine 侧 before_llm_call 才能识别并展开。
    this._ta.focus();
    this._ta.setSelectionRange(S.tokenStart + 1, S.tokenEnd);
    await this._ctx.api.call('insertComposerText', path + ' ');
    this._close();
  },

  _close() {
    const S = this._ctx?.state;
    if (!S) return;
    S.open = false;
    S.items = [];
    S.hl = 0;
    this._render();
  },

  _render() {
    const S = this._ctx?.state;
    if (!this._ui || !S) return;
    if (!S.open) {
      this._ui.style.display = 'none';
      this._ui.innerHTML = '';
      return;
    }
    const items = S.items || [];
    const rows = items.length
      ? items.map((p, idx) => {
          const row = document.createElement('button');
          row.type = 'button';
          row.className = 'ide-completer-row' + (idx === S.hl ? ' active' : '');
          const dot = document.createElement('span');
          dot.className = 'ide-completer-icon';
          dot.textContent = '📄';
          const label = document.createElement('span');
          label.className = 'ide-completer-path';
          label.textContent = p;
          label.title = p;
          row.appendChild(dot);
          row.appendChild(label);
          row.addEventListener('mousedown', (e) => e.preventDefault());
          row.addEventListener('click', () => void this._pick(p));
          return row;
        })
      : [(() => {
          const empty = document.createElement('div');
          empty.className = 'ide-completer-empty';
          empty.textContent = S.files ? '无匹配文件' : '加载文件列表…';
          return empty;
        })()];
    this._ui.innerHTML = '';
    for (const r of rows) this._ui.appendChild(r);
    this._ui.style.display = 'block';
  },
};

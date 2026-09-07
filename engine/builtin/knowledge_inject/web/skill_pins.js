// knowledge_inject.skill_pins — 输入框上方的工作区技能挂载 chip（input_above 槽位）。
// 数据流：以宿主传入的 sessionId 调 GET /v1/admin/config/skill_pins?session_id=，
// 工作区解析在 supervisor 侧完成；chip 显示当前工作区已挂载的技能数，点击展开
// 浮层勾选，变更立即 PUT 整个 pinned 列表。私密（visibility=private）技能不会
// 被 constant/关键词自动注入，只在挂载后进入上下文，浮层中以「私密」标注。

const CSS = `
.ki-pin-chip {
  pointer-events: auto;
  border: 1px solid var(--duties-border, #d4d4d4);
  background: var(--duties-bg, #fff);
  color: var(--duties-secondary, #666);
  font-family: inherit;
  font-size: 11px;
  line-height: 1;
  padding: 4px 8px;
  cursor: pointer;
  border-radius: 3px;
  white-space: nowrap;
}
.ki-pin-chip:hover { color: var(--duties-text, #222); border-color: var(--duties-text, #222); }
.ki-pin-chip[data-active="1"] { color: var(--duties-text, #222); border-color: var(--duties-text, #222); }
.ki-pin-pop {
  position: absolute;
  bottom: calc(100% + 4px);
  right: 0;
  width: 300px;
  max-height: 340px;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--duties-border, #d4d4d4);
  background: var(--duties-bg, #fff);
  box-shadow: 0 4px 16px rgba(0,0,0,0.12);
  z-index: 30;
  font-size: 12px;
  color: var(--duties-text, #222);
}
.ki-pin-head {
  padding: 6px 8px;
  border-bottom: 1px solid var(--duties-border, #d4d4d4);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ki-pin-ws { font-family: monospace; font-size: 10px; color: var(--duties-tertiary, #999); }
.ki-pin-search {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--duties-border, #d4d4d4);
  background: transparent;
  color: inherit;
  font-size: 11px;
  padding: 4px 6px;
  outline: none;
}
.ki-pin-list { overflow-y: auto; flex: 1; }
.ki-pin-row {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 5px 8px;
  cursor: pointer;
}
.ki-pin-row:hover { background: var(--duties-muted, #f5f5f5); }
.ki-pin-row input { margin-top: 2px; flex: none; }
.ki-pin-name { font-family: monospace; font-size: 11px; word-break: break-all; }
.ki-pin-desc { font-size: 10px; color: var(--duties-tertiary, #999); margin-top: 1px; }
.ki-pin-tag {
  flex: none;
  font-size: 9px;
  border: 1px solid var(--duties-border, #d4d4d4);
  color: var(--duties-tertiary, #999);
  padding: 0 3px;
  margin-left: 4px;
  vertical-align: 1px;
}
.ki-pin-empty { padding: 12px; text-align: center; color: var(--duties-tertiary, #999); }
.ki-pin-err { padding: 4px 8px; font-size: 10px; color: #b45309; }
`;

export default {
  mount(ctx) {
    const S = ctx.state;
    S.sessionId = '';
    S.workspace = '';
    S.skills = [];
    S.open = false;
    S.loading = false;
    S.error = '';
    S.filter = '';
    this._ctx = ctx;

    const style = document.createElement('style');
    style.textContent = CSS;
    ctx.el.appendChild(style);

    this._wrap = document.createElement('span');
    this._wrap.style.position = 'relative';
    this._wrap.style.display = 'none';

    this._chip = document.createElement('button');
    this._chip.type = 'button';
    this._chip.className = 'ki-pin-chip';
    this._chip.addEventListener('click', () => this._toggle());
    this._wrap.appendChild(this._chip);
    ctx.el.appendChild(this._wrap);

    this._onDocDown = (e) => {
      if (!S.open) return;
      if (this._wrap.contains(e.target)) return;
      S.open = false;
      this._render();
    };
    document.addEventListener('mousedown', this._onDocDown);

    // [2026-09-05] Why: the host calls mount right after import and only calls
    // update when list/data changes afterwards; a contribution that waits for
    // update to read its first sessionId may never render. How: load eagerly
    // from ctx.data at mount. Purpose: the chip appears on first paint instead
    // of after the next unrelated data change.
    const sid = (ctx.data && ctx.data.sessionId) || '';
    if (sid && sid !== S.sessionId) void this._load(sid);
    this._render();
  },

  update(ctx) {
    const prev = this._ctx?.data?.sessionId;
    this._ctx = ctx;
    const next = ctx.data?.sessionId || '';
    if (prev !== next) {
      this._wrap.style.display = 'none';
      ctx.state.open = false;
      if (next) void this._load(next);
      else this._ctx.state.workspace = '';
    }
    this._render();
  },

  destroy() {
    document.removeEventListener('mousedown', this._onDocDown);
  },

  async _load(sessionId) {
    const S = this._ctx.state;
    S.sessionId = sessionId;
    S.loading = true;
    S.error = '';
    this._render();
    try {
      const resp = await this._ctx.api.request(
        '/admin/config/skill_pins?session_id=' + encodeURIComponent(sessionId),
      );
      if (S.sessionId !== sessionId) return;
      S.workspace = (resp && resp.workspace) || '';
      S.skills = (resp && Array.isArray(resp.skills)) ? resp.skills : [];
    } catch (e) {
      if (S.sessionId !== sessionId) return;
      S.workspace = '';
      S.skills = [];
      S.error = '加载失败';
    } finally {
      if (S.sessionId === sessionId) S.loading = false;
      this._render();
    }
  },

  _toggle() {
    const S = this._ctx.state;
    S.open = !S.open;
    this._render();
  },

  async _setPinned(name, on) {
    const S = this._ctx.state;
    const pinned = S.skills.filter((s) => s.pinned).map((s) => s.name);
    const next = on
      ? [...pinned, name]
      : pinned.filter((n) => n !== name);
    const skill = S.skills.find((s) => s.name === name);
    if (skill) skill.pinned = on;
    S.error = '';
    this._render();
    try {
      const resp = await this._ctx.api.request('/admin/config/skill_pins', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: S.sessionId, pinned: next }),
      });
      if (resp && resp.ok === false) throw new Error('rejected');
    } catch (e) {
      if (skill) skill.pinned = !on;
      S.error = '保存失败';
      this._render();
    }
  },

  _render() {
    const S = this._ctx?.state;
    if (!S || !this._chip) return;
    if (!S.workspace) {
      this._wrap.style.display = 'none';
      return;
    }
    this._wrap.style.display = '';
    const count = S.skills.filter((s) => s.pinned).length;
    this._chip.textContent = S.loading
      ? '技能挂载 …'
      : (count > 0 ? `技能挂载 ×${count}` : '技能挂载');
    this._chip.dataset.active = count > 0 ? '1' : '0';

    const old = this._wrap.querySelector('.ki-pin-pop');
    if (old) old.remove();
    if (!S.open) return;

    const pop = document.createElement('div');
    pop.className = 'ki-pin-pop';

    const head = document.createElement('div');
    head.className = 'ki-pin-head';
    const ws = document.createElement('div');
    ws.className = 'ki-pin-ws';
    ws.textContent = '工作区：' + S.workspace;
    const search = document.createElement('input');
    search.className = 'ki-pin-search';
    search.placeholder = '搜索技能…';
    search.value = S.filter;
    search.addEventListener('input', () => {
      S.filter = search.value;
      this._renderList(list);
    });
    head.append(ws, search);
    pop.appendChild(head);

    const list = document.createElement('div');
    list.className = 'ki-pin-list';
    pop.appendChild(list);
    this._renderList(list);

    if (S.error) {
      const err = document.createElement('div');
      err.className = 'ki-pin-err';
      err.textContent = S.error;
      pop.appendChild(err);
    }

    this._wrap.appendChild(pop);
    search.focus();
  },

  _renderList(list) {
    const S = this._ctx.state;
    list.textContent = '';
    const q = S.filter.trim().toLowerCase();
    const rows = S.skills.filter((s) =>
      !q || s.name.toLowerCase().includes(q) || (s.description || '').toLowerCase().includes(q),
    );
    rows.sort((a, b) => (Number(b.pinned) - Number(a.pinned)) || a.name.localeCompare(b.name));
    if (rows.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'ki-pin-empty';
      empty.textContent = S.loading ? '加载中…' : '无匹配技能';
      list.appendChild(empty);
      return;
    }
    for (const s of rows) {
      const row = document.createElement('label');
      row.className = 'ki-pin-row';
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = Boolean(s.pinned);
      box.addEventListener('change', () => void this._setPinned(s.name, box.checked));
      const text = document.createElement('span');
      const name = document.createElement('div');
      name.className = 'ki-pin-name';
      name.textContent = s.name;
      if (s.visibility === 'private') {
        const tag = document.createElement('span');
        tag.className = 'ki-pin-tag';
        tag.textContent = '私密';
        name.appendChild(tag);
      }
      text.appendChild(name);
      if (s.description) {
        const desc = document.createElement('div');
        desc.className = 'ki-pin-desc';
        desc.textContent = s.description.length > 60 ? s.description.slice(0, 60) + '…' : s.description;
        text.appendChild(desc);
      }
      row.append(box, text);
      list.appendChild(row);
    }
  },
};

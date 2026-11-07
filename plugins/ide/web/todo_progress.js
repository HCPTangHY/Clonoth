/* Read-only model work plan. The chat slot owns placement; tools own updates. */
const LABELS = {
  pending: '待处理', in_progress: '进行中', completed: '已完成', blocked: '受阻', cancelled: '已取消',
};
const MARKERS = { pending: '○', in_progress: '›', completed: '✓', blocked: '!', cancelled: '–' };
const CSS = `
.ide-todo-progress { --todo-muted:color-mix(in srgb,var(--duties-tertiary,#6e6e73) 90%,var(--duties-text,#252525)); box-sizing:border-box; width:640px; max-width:calc(100% - 32px); margin:10px auto 0; pointer-events:none; border:0; border-radius:0; background:transparent; color:var(--duties-text); box-shadow:none; font-size:12px; line-height:1.5; }
.ide-todo-progress[hidden], .ide-todo-progress [hidden] { display:none; }
.ide-todo-progress [data-status] { --todo-color:var(--duties-secondary,#4a4a4a); }
.ide-todo-progress [data-status="in_progress"] { --todo-color:var(--duties-info,#2563eb); }
.ide-todo-progress [data-status="completed"] { --todo-color:var(--duties-success,#267045); }
.ide-todo-progress [data-status="blocked"] { --todo-color:var(--duties-warning,#a04b13); }
.ide-todo-progress [data-status="cancelled"] { --todo-color:var(--todo-muted); }
.ide-todo-progress__summary { box-sizing:border-box; display:flex; align-items:center; gap:12px; min-width:0; padding:0 12px; border:1px solid var(--duties-border); background:var(--duties-bg); pointer-events:auto; }
.ide-todo-progress__toggle { box-sizing:border-box; display:inline-flex; flex-shrink:0; align-items:center; gap:6px; min-height:34px; padding:4px 0; pointer-events:auto; border:0; border-radius:0; background:transparent; color:var(--todo-muted); font:11px/1.5 'Geist Mono',ui-monospace,monospace; cursor:pointer; text-align:left; }
.ide-todo-progress__toggle:hover { color:var(--duties-text); }
.ide-todo-progress__toggle:hover .ide-todo-progress__count { text-decoration:underline; text-underline-offset:3px; }
.ide-todo-progress__toggle:focus-visible { outline:2px solid var(--duties-accent); outline-offset:2px; }
.ide-todo-progress__count { white-space:nowrap; }
.ide-todo-progress__current { display:flex; align-items:center; gap:8px; flex:1; min-width:0; color:var(--todo-color); }
.ide-todo-progress__status { flex-shrink:0; width:10px; text-align:center; font:12px/1.5 ui-monospace,monospace; }
.ide-todo-progress__attention { flex-shrink:0; font:10px/1.5 'Geist Mono',ui-monospace,monospace; white-space:nowrap; }
.ide-todo-progress__subject { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.ide-todo-progress__attention { color:var(--todo-color); }
.ide-todo-progress__current[data-status="in_progress"] .ide-todo-progress__subject { font-weight:600; }
.ide-todo-progress__body { box-sizing:border-box; width:100%; max-height:min(28dvh,240px); margin-top:6px; overflow-y:auto; overscroll-behavior:contain; pointer-events:auto; border:1px solid var(--duties-border); border-radius:0; background:var(--duties-bg); padding:2px 12px 10px; }
.ide-todo-progress .todo-list { list-style:none; padding:0; margin:0; }
.ide-todo-progress .todo-row { padding:8px 0; overflow-wrap:anywhere; }
.ide-todo-progress .todo-heading { display:grid; grid-template-columns:14px minmax(0,1fr) auto; gap:8px; align-items:baseline; color:var(--todo-color); }
.ide-todo-progress .todo-title { margin:0; }
.ide-todo-progress .todo-marker { text-align:center; font-family:ui-monospace,monospace; }
.ide-todo-progress .todo-state { white-space:nowrap; font:10px/1.5 'Geist Mono',ui-monospace,monospace; }
.ide-todo-progress .todo-row[data-status="in_progress"] .todo-title { font-weight:600; }
.ide-todo-progress .todo-row[data-status="cancelled"] .todo-title { text-decoration:line-through; }
.ide-todo-progress .todo-note { color:var(--todo-muted); font-size:11px; white-space:pre-wrap; overflow-wrap:anywhere; margin:3px 0 0 22px; }
.ide-todo-progress .todo-hint { color:var(--todo-muted); font-size:10px; margin:8px 0 0; }
@media(max-width:480px) { .ide-todo-progress { max-width:calc(100% - 24px); margin-top:8px; } .ide-todo-progress__toggle { min-height:38px; } .ide-todo-progress__summary { gap:8px; padding-inline:8px; } .ide-todo-progress__current { display:grid; grid-template-columns:10px minmax(0,1fr); gap:0 6px; padding-block:4px; } .ide-todo-progress__attention { grid-column:2; } .ide-todo-progress__body { padding-inline:8px; } }
`;
function element(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

export default {
  mount(ctx) {
    this.ctx = ctx;
    this.disposed = false;
    this.seq = 0;
    this.sessionId = '';
    this.isChild = false;
    this.data = null;
    this.controller = null;
    this.style = element('style', '', CSS);
    this.root = element('section', 'ide-todo-progress');
    this.root.setAttribute('aria-label', '当前会话待办');
    this.root.hidden = true;
    this.chevron = element('span', '', '▸');
    this.chevron.setAttribute('aria-hidden', 'true');
    this.count = element('span', 'ide-todo-progress__count');
    this.summary = element('div', 'ide-todo-progress__summary');
    this.current = element('div', 'ide-todo-progress__current');
    this.status = element('span', 'ide-todo-progress__status');
    this.status.setAttribute('role', 'img');
    this.subject = element('span', 'ide-todo-progress__subject');
    this.attention = element('span', 'ide-todo-progress__attention');
    this.attention.hidden = true;
    this.current.append(this.status, this.subject, this.attention);
    this.toggle = element('button', 'ide-todo-progress__toggle');
    this.toggle.type = 'button';
    this.toggle.append(this.chevron, this.count);
    this.summary.append(this.toggle, this.current);
    this.toggle.addEventListener('click', () => {
      if (!this.data) { void this.load(); return; }
      this.ctx.state.expanded = !this.ctx.state.expanded;
      this.render();
    });
    this.body = element('div', 'ide-todo-progress__body');
    // UI ID only; works on HTTP deployments without secure-context crypto.
    this.body.id = 'ide-todos-' + Math.random().toString(36).slice(2);
    this.toggle.setAttribute('aria-controls', this.body.id);
    this.root.append(this.summary, this.body);
    ctx.el.append(this.style, this.root);
    this.root.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || !this.ctx.state.expanded) return;
      event.stopPropagation();
      this.ctx.state.expanded = false;
      this.render();
      this.toggle.focus();
    });
    this.onPointerDown = event => {
      if (!this.ctx.state.expanded || this.root.contains(event.target)) return;
      this.ctx.state.expanded = false;
      this.render();
    };
    document.addEventListener('pointerdown', this.onPointerDown);
    this.onVisibility = () => { if (!document.hidden) void this.load(); };
    document.addEventListener('visibilitychange', this.onVisibility);
    // ponytail: read at most once per four seconds while visible. Empty lists
    // still poll so the card appears when the model creates its first plan.
    this.timer = window.setInterval(() => { if (!document.hidden) void this.load(); }, 4000);
    this.update(ctx); // Initial mount must not depend on a later host update.
  },

  update(ctx) {
    this.ctx = ctx;
    const sid = String(ctx.data?.sessionId || '');
    const child = Boolean(ctx.data?.isChildSession);
    if (sid === this.sessionId && child === this.isChild) return;
    this.sessionId = sid;
    this.isChild = child;
    ++this.seq;
    this.controller?.abort();
    this.controller = null;
    this.data = null;
    this.root.hidden = true;
    this.body.replaceChildren();
    if (ctx.state.sessionId !== sid || child) {
      ctx.state.sessionId = sid;
      ctx.state.workspacePath = '';
      ctx.state.expanded = false;
    }
    if (sid && !child) void this.load();
  },

  async load() {
    if (this.disposed || !this.sessionId || this.isChild || this.controller) return;
    const seq = ++this.seq;
    const controller = new AbortController();
    this.controller = controller;
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    try {
      const data = await this.ctx.api.request('/plugins/ide/todos?session_id=' + encodeURIComponent(this.sessionId), {
        signal: controller.signal,
      });
      if (this.disposed || seq !== this.seq) return;
      if (!data || !Array.isArray(data.items) || typeof data.workspace?.path !== 'string') {
        throw new Error('待办响应格式无效');
      }
      const unchanged = this.data?.revision === data.revision
        && this.data?.workspace.path === data.workspace.path;
      if (this.ctx.state.workspacePath !== data.workspace.path) this.ctx.state.expanded = false;
      this.ctx.state.workspacePath = data.workspace.path;
      this.data = data;
      if (!unchanged) this.render();
    } catch {
      if (this.disposed || seq !== this.seq) return;
      // Do not turn a failed read into an empty list. If previously displayed,
      // replace stale details with a small retry notice; first-load errors hide.
      this.data = null;
      this.body.replaceChildren();
      this.body.hidden = true;
      this.toggle.setAttribute('aria-expanded', 'false');
      this.chevron.textContent = '▸';
      this.count.textContent = '待办读取失败';
      this.current.dataset.status = 'blocked';
      this.status.textContent = '!';
      this.status.setAttribute('aria-label', '读取失败');
      this.subject.textContent = '点击重试';
      this.attention.hidden = true;
      this.current.title = '';
      this.toggle.setAttribute('aria-label', '待办读取失败，点击重试');
    } finally {
      window.clearTimeout(timeout);
      if (this.controller === controller) this.controller = null;
    }
  },

  render() {
    const items = this.data?.items || [];
    this.root.hidden = items.length === 0;
    if (!items.length) {
      this.ctx.state.expanded = false;
      this.body.replaceChildren();
      this.body.hidden = true;
      this.toggle.setAttribute('aria-expanded', 'false');
      return;
    }
    const done = items.filter(i => i.status === 'completed').length;
    const blocked = items.filter(i => i.status === 'blocked');
    const current = items.find(i => i.status === 'in_progress') || blocked[0] || items.find(i => i.status === 'pending');
    const cancelled = items.filter(i => i.status === 'cancelled').length;
    this.count.textContent = `待办 ${done}/${items.length}`;
    const status = current?.status || (done === items.length ? 'completed' : 'cancelled');
    this.current.dataset.status = status;
    this.status.textContent = MARKERS[status];
    this.status.setAttribute('aria-label', LABELS[status]);
    this.subject.textContent = current?.title || (done === items.length ? '全部事项已完成' : '清单无未完成项');
    this.attention.hidden = !blocked.length && !cancelled;
    this.attention.dataset.status = blocked.length ? 'blocked' : 'cancelled';
    this.attention.textContent = [blocked.length ? `受阻 ${blocked.length} 项` : '',
      cancelled ? `已取消 ${cancelled} 项` : ''].filter(Boolean).join(' · ');
    this.current.title = [
      `${LABELS[status]}：${this.subject.textContent}`,
      blocked.length ? `受阻 ${blocked.length} 项：${blocked[0].note || blocked[0].title}` : '',
      cancelled ? `已取消 ${cancelled} 项` : '',
    ].filter(Boolean).join(' · ');
    this.toggle.setAttribute('aria-label', `${this.count.textContent} 已完成，${this.current.title}。展开或收起清单`);
    const expanded = Boolean(this.ctx.state.expanded);
    this.toggle.setAttribute('aria-expanded', String(expanded));
    this.chevron.textContent = expanded ? '▾' : '▸';
    this.body.hidden = !expanded;
    const scroll = this.body.scrollTop;
    this.body.replaceChildren();
    if (!expanded) return;
    const list = element('ol', 'todo-list');
    for (const [index, item] of items.entries()) {
      const row = element('li', 'todo-row');
      row.dataset.status = item.status;
      const heading = element('div', 'todo-heading');
      const marker = element('span', 'todo-marker', MARKERS[item.status]);
      marker.setAttribute('aria-hidden', 'true');
      heading.append(marker, element('p', 'todo-title', `${index + 1}. ${item.title}`),
        element('span', 'todo-state', LABELS[item.status]));
      row.append(heading);
      if (item.note) row.append(element('p', 'todo-note', item.note));
      list.append(row);
    }
    this.body.append(list, element('p', 'todo-hint', '计划由模型维护，可通过对话要求调整。待办完成不代表 Goal 已通过验收。'));
    this.body.scrollTop = scroll;
  },

  destroy() {
    this.disposed = true;
    ++this.seq;
    window.clearInterval(this.timer);
    this.controller?.abort();
    document.removeEventListener('visibilitychange', this.onVisibility);
    document.removeEventListener('pointerdown', this.onPointerDown);
    this.data = null;
    this.root?.remove();
    this.style?.remove();
  },
};

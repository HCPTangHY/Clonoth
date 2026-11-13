/* Only a compact status entry belongs in the chat header; details use the right rail. */
const LABELS = { empty: '未设置', paused: '已暂停', running: '执行中', waiting: '等待结果', reviewing: '评审中', completed: '已完成', blocked: '需要处理' };
export default {
  mount(ctx) {
    this.ctx = ctx;
    this.sessionId = ''; // Raw viewed session; never the selected sidebar conversation.
    this.ownerSessionId = '';
    this.seq = 0;
    this.disposed = false;
    this.button = document.createElement('button');
    this.button.type = 'button';
    this.button.className = 'ide-goal-entry';
    this.button.hidden = true;
    this.button.setAttribute('aria-label', '打开 Goal 详情');
    this.button.addEventListener('click', () => void this.ctx.api.call('openPanel', 'plugin:ide:goal'));
    this.style = document.createElement('style');
    this.style.textContent = `
.ide-goal-entry { display:inline-flex; align-items:center; gap:8px; min-height:28px; padding:4px 8px; border:0; border-radius:0; background:transparent; color:var(--duties-secondary); font:inherit; font-family:'Geist Mono',ui-monospace,monospace; font-size:12px; line-height:20px; white-space:nowrap; cursor:pointer; transition:background-color 150ms,color 150ms; }
.ide-goal-entry::before { content:''; width:6px; height:6px; background:var(--duties-tertiary); flex-shrink:0; }
.ide-goal-entry[data-status=running]::before,.ide-goal-entry[data-status=waiting]::before,.ide-goal-entry[data-status=reviewing]::before { background:var(--duties-accent); }
.ide-goal-entry[data-status=completed]::before { background:var(--duties-text); }
.ide-goal-entry:hover { background:var(--duties-panel); color:var(--duties-text); }
.ide-goal-entry:focus-visible { outline:2px solid var(--duties-text); outline-offset:2px; }
.ide-goal-entry[hidden] { display:none; }
@media(max-width:767px) { .ide-goal-entry { display:none; } }
@media(prefers-reduced-motion:reduce) { .ide-goal-entry { transition:none; } }
`;
    ctx.el.append(this.style, this.button);
    this.unsubscribe = ctx.events?.on('ide_goal_updated', payload => {
      if (payload?.session_id === (this.ownerSessionId || this.sessionId)) { this.pending = true; void this.load(); }
    });
    this.onVisibility = () => { if (!document.hidden) void this.load(); };
    document.addEventListener('visibilitychange', this.onVisibility);
    this.timer = window.setInterval(this.onVisibility, 4000);
    this.update(ctx);
  },
  update(ctx) {
    this.ctx = ctx;
    const sid = String(ctx.data?.sessionId || '');
    if (sid === this.sessionId) return;
    this.controller?.abort();
    this.controller = null;
    ++this.seq;
    this.sessionId = sid;
    this.ownerSessionId = '';
    this.button.hidden = !sid || sid === 'no-session';
    this.button.disabled = true;
    this.button.textContent = 'Goal · 读取中';
    this.pending = false;
    if (sid && sid !== 'no-session') void this.load();
  },
  async load() {
    if (this.disposed || !this.sessionId || this.controller) return;
    const seq = ++this.seq;
    const controller = new AbortController();
    this.controller = controller;
    this.pending = false;
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    try {
      const data = await this.ctx.api.request('/plugins/ide/goal?session_id=' + encodeURIComponent(this.sessionId), { signal: controller.signal });
      if (this.disposed || seq !== this.seq) return;
      if (!data || typeof data.session_id !== 'string' || !data.session_id
          || (data.session_id !== this.sessionId && data.requested_session_id !== this.sessionId)
          || typeof data.is_child !== 'boolean' || !Object.hasOwn(LABELS, data.status)) throw new Error('Goal 响应无效');
      this.ownerSessionId = data.session_id;
      this.button.hidden = !data.goal || data.is_child;
      this.button.disabled = data.is_child;
      this.button.dataset.status = data.status;
      this.button.textContent = 'Goal · ' + LABELS[data.status];
      this.button.title = this.ownerSessionId !== this.sessionId
        ? `此 Goal 属于主会话：${this.ownerSessionId}。查看目标、评审记录与设置。` : '查看目标、评审记录与设置';
    } catch (error) {
      if (this.disposed || seq !== this.seq) return;
      this.button.hidden = false;
      this.button.disabled = false;
      this.button.dataset.status = 'blocked';
      this.button.textContent = 'Goal · 读取失败';
      this.button.title = '打开详情重新读取';
    } finally {
      window.clearTimeout(timeout);
      if (this.controller === controller) {
        this.controller = null;
        if (this.pending) void this.load();
      }
    }
  },
  destroy() {
    this.disposed = true;
    ++this.seq;
    this.controller?.abort();
    window.clearInterval(this.timer);
    document.removeEventListener('visibilitychange', this.onVisibility);
    this.unsubscribe?.();
    this.button.remove();
    this.style.remove();
  },
};

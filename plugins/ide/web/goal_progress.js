/* Session-owned Goal controls. Backend state, never workspace names, owns identity. */
const LABELS = { empty: '未设置', paused: '已暂停', running: '执行中', waiting: '等待结果', reviewing: '评审中', completed: '已完成', blocked: '需要处理' };
const ACTIVE = new Set(['running', 'waiting', 'reviewing']);
let sequence = 0;
function requestId() {
  return typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID() : `goal-${Date.now()}-${++sequence}-${Math.random().toString(36).slice(2)}`;
}
function element(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}
const CSS = `
.ide-goal { box-sizing:border-box; width:100%; min-width:0; padding:20px 16px; color:var(--duties-text); font:14px/1.6 Inter,ui-sans-serif,system-ui,sans-serif; --goal-muted:var(--duties-secondary); }
.ide-goal *, .ide-goal *::before, .ide-goal *::after { box-sizing:border-box; }
.ide-goal[hidden], .ide-goal [hidden] { display:none; }
.ide-goal__summary { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:12px; }
.ide-goal__title,.ide-goal__section-title { margin:0; font:500 12px/1.5 'Geist Mono',ui-monospace,monospace; color:var(--duties-secondary); }
.ide-goal__badge { display:inline-flex; align-items:center; gap:8px; font:12px/1.5 'Geist Mono',ui-monospace,monospace; }
.ide-goal__badge::before { content:''; width:6px; height:6px; background:var(--duties-tertiary); }
.ide-goal[data-status=running] .ide-goal__badge::before,.ide-goal[data-status=reviewing] .ide-goal__badge::before,.ide-goal[data-status=waiting] .ide-goal__badge::before { background:var(--duties-accent); }
.ide-goal[data-status=completed] .ide-goal__badge::before { background:var(--duties-text); }
.ide-goal__objective { margin:0 0 12px; white-space:pre-wrap; overflow-wrap:anywhere; font-size:14px; }
.ide-goal__meta,.ide-goal__hint { margin:0 0 16px; color:var(--duties-tertiary); font-size:12px; line-height:1.6; white-space:pre-wrap; overflow-wrap:anywhere; }
.ide-goal__criteria { margin:8px 0 0; font-size:12px; white-space:pre-wrap; overflow-wrap:anywhere; color:var(--duties-secondary); }
.ide-goal__section { margin-top:24px; padding-top:16px; border-top:1px solid color-mix(in srgb,var(--duties-border) 60%,transparent); }
.ide-goal__form { display:grid; gap:12px; margin-top:16px; }
.ide-goal__label { display:grid; gap:4px; color:var(--duties-secondary); font-size:12px; }
.ide-goal__field { display:block; width:100%; min-height:36px; padding:8px; border:1px solid color-mix(in srgb,var(--duties-border) 70%,transparent); border-radius:0; background:var(--duties-bg); color:var(--duties-text); font:inherit; font-size:12px; transition:border-color 150ms; }
textarea.ide-goal__field { resize:vertical; min-height:80px; }
.ide-goal__field:focus-visible,.ide-goal__button:focus-visible,.ide-goal__settings summary:focus-visible { outline:2px solid var(--duties-text); outline-offset:2px; }
.ide-goal__models { display:grid; gap:12px; }
.ide-goal__actions { display:flex; flex-wrap:wrap; gap:8px; }
.ide-goal__button { display:inline-flex; align-items:center; justify-content:center; min-height:32px; padding:4px 12px; border:1px solid color-mix(in srgb,var(--duties-border) 60%,transparent); border-radius:0; color:var(--duties-secondary); background:transparent; font:12px/1.5 'Geist Mono',ui-monospace,monospace; cursor:pointer; transition:background-color 150ms,color 150ms; }
.ide-goal__button:hover:not(:disabled) { background:var(--duties-panel); color:var(--duties-text); }
.ide-goal__button:active:not(:disabled) { transform:scale(.98); }
.ide-goal__button:disabled { opacity:.5; cursor:default; }
.ide-goal__button--primary { background:var(--duties-panel); color:var(--duties-text); }
.ide-goal__notice { margin:0 0 16px; padding:12px; background:var(--duties-panel); color:var(--duties-secondary); font-size:12px; white-space:pre-wrap; overflow-wrap:anywhere; }
.ide-goal__confirm { margin-top:16px; padding:12px; background:var(--duties-panel); }
.ide-goal__reviews { list-style:none; padding:0; margin:16px 0 0; }
.ide-goal__review { position:relative; margin-left:4px; padding:0 0 20px 16px; border-left:1px solid color-mix(in srgb,var(--duties-border) 60%,transparent); font-size:12px; white-space:pre-wrap; overflow-wrap:anywhere; color:var(--duties-secondary); }
.ide-goal__review::before { content:''; position:absolute; top:6px; left:-3px; width:5px; height:5px; background:var(--duties-tertiary); }
.ide-goal__review:last-child { padding-bottom:0; border-left-color:transparent; }
.ide-goal__review-title { display:block; margin-bottom:8px; font:500 12px/1.5 'Geist Mono',ui-monospace,monospace; color:var(--duties-text); }
.ide-goal__settings summary { cursor:pointer; color:var(--duties-secondary); font:12px/1.5 'Geist Mono',ui-monospace,monospace; }
.ide-goal__settings .ide-goal__actions { margin-top:16px; }
.ide-goal__skeleton { width:100%; height:12px; margin:8px 0; background:var(--duties-muted); animation:ide-goal-pulse 1.5s ease-in-out infinite; }
@keyframes ide-goal-pulse { 50% { opacity:.45; } }
@media(prefers-reduced-motion:reduce) { .ide-goal * { animation:none; transition:none; } }
`;

export default {
  mount(ctx) {
    this.ctx = ctx;
    this.disposed = false;
    this.seq = 0;
    this.sessionId = ''; // Raw view identity is used for reads and composer metadata.
    this.ownerSessionId = ''; // Canonical Goal identity is used only for mutations.
    this.isChild = false;
    this.data = null;
    this.error = '';
    this.dirty = false;
    this.conflict = false;
    this.mutating = false;
    this.controller = null;
    this.timeout = null;
    this.pendingRefresh = false;
    this.style = element('style', '', CSS);
    this.root = element('section', 'ide-goal');
    this.root.setAttribute('aria-label', '当前会话 Goal');
    this.root.hidden = true;
    const card = element('div', 'ide-goal__card');
    const summary = element('div', 'ide-goal__summary');
    this.title = element('h2', 'ide-goal__title', '当前目标');
    this.badge = element('span', 'ide-goal__badge', '读取中');
    this.body = element('div', 'ide-goal__body');
    summary.append(this.title, this.badge);
    this.notice = element('p', 'ide-goal__notice');
    this.notice.setAttribute('role', 'status');
    this.notice.setAttribute('aria-live', 'polite');
    this.skeleton = element('div', 'ide-goal__skeleton');
    this.skeleton.setAttribute('aria-hidden', 'true');
    this.meta = element('p', 'ide-goal__meta');
    this.form = element('div', 'ide-goal__form');
    this.goalText = element('p', 'ide-goal__objective');
    this.edit = this.button('修改目标', async () => {
      const sessionId = this.sessionId;
      const data = this.data;
      const ownerSessionId = this.ownerSessionId;
      const version = this.draftVersion;
      if (!data || this.isChild || this.error) return;
      const current = () => !this.disposed && sessionId === this.sessionId && version === this.draftVersion;
      try {
        // Reopening a running Goal must not replace an unsent mobile draft.
        const saved = this.mobile && this.compose.hidden ? await this.ctx.composer?.getDraft?.() : null;
        if (!current()) return;
        const resumeDraft = Boolean(saved?.text.trim());
        const accepted = await this.ctx.composer?.setMode('ide.goal', resumeDraft ? saved.text : data.goal,
          { sessionId, ownerSessionId, revision: data.revision,
            action: ['empty', 'completed'].includes(data.status) ? 'create' : 'submit', focus: !this.mobile });
        if (accepted === true && current()) {
          this.editingGoal = true;
          ++this.draftVersion;
          this.draftError = '';
          this.draftWriteFailed = false;
          this.draft.value = resumeDraft ? saved.text : data.goal;
          await this.syncDraft();
          if (this.mobile) this.draft.focus();
        }
      } catch (error) {
        if (!this.disposed && sessionId === this.sessionId) { this.draftError = error.message; this.renderDraft(); }
      }
    });
    this.compose = element('section', 'ide-goal__form');
    const draftLabel = element('label', 'ide-goal__label', '目标正文');
    this.draft = element('textarea', 'ide-goal__field');
    this.draft.maxLength = 16000;
    this.draft.style.fontSize = '16px';
    this.draft.placeholder = '描述目标、限制和完成条件…';
    this.draft.rows = 5;
    draftLabel.append(this.draft);
    this.draftVersion = 0;
    this.editingGoal = false;
    this.draftError = '';
    this.draftSnapshot = null;
    this.mobile = (ctx.data?.viewportWidth?.() ?? 1024) < 768;
    this.draftWrite = Promise.resolve();
    this.draft.addEventListener('input', () => {
      ++this.draftVersion;
      const text = this.draft.value;
      this.draftError = '';
      this.renderDraft();
      this.draftWriteFailed = false;
      const sessionId = this.sessionId;
      const version = this.draftVersion;
      this.draftWrite = this.ctx.composer.setDraft(text).then(accepted => {
        if (accepted !== true) throw new Error('当前输入不可修改，正文已保留，请重新打开面板核对。');
      }).catch(error => {
        if (!this.disposed && sessionId === this.sessionId && version === this.draftVersion) {
          this.draftWriteFailed = true; this.draftError = error.message; this.renderDraft();
        }
      });
    });
    this.submitDraft = this.button('开始目标', () => { void this.submitGoal(); }, true);
    this.retryDraft = this.button('核对最新版本', async () => {
      const sessionId = this.sessionId;
      const version = this.draftVersion;
      const current = () => !this.disposed && sessionId === this.sessionId && version === this.draftVersion;
      if (!this.data || this.isChild || !window.confirm('将按当前状态重新选择新建或修改；新建会替换已完成目标并重置预算和专属记录。请确认已核对目标状态。')) return;
      try {
        const accepted = await this.ctx.composer.setMode('ide.goal', this.draft.value,
          { sessionId, ownerSessionId: this.ownerSessionId, revision: this.data.revision,
            action: ['empty', 'completed'].includes(this.data.status) ? 'create' : 'submit', resetSubmission: true, focus: false });
        if (accepted === true && current()) {
          ++this.draftVersion;
          this.draftError = '';
          this.draftWriteFailed = false;
          await this.syncDraft();
        }
      } catch (error) { if (current()) this.draftError = error.message; }
      if (!this.disposed && sessionId === this.sessionId) this.renderDraft();
    });
    this.draftNotice = element('p', 'ide-goal__hint');
    this.draftNotice.setAttribute('role', 'status');
    this.desktopHint = element('p', 'ide-goal__hint', 'Goal 侧栏打开时，输入框用于提交目标，不是发送聊天消息。未完成时修改目标并保留预算；已完成时创建独立目标，重置预算和专属记录。关闭侧栏后恢复普通对话。');
    this.compose.append(draftLabel, this.submitDraft, this.retryDraft, this.draftNotice);
    if (ctx.composer?.getDraft) this.draftTimer = window.setInterval(() => { if (!document.hidden) void this.syncDraft(); }, 400);
    this.criteriaText = element('p', 'ide-goal__criteria');
    this.criteria = this.field('验收条件', 'textarea', 16000);
    const models = element('div', 'ide-goal__models');
    this.provider = this.field('评审渠道（留空跟随本轮）', 'input', 200, models);
    this.model = this.field('评审模型（留空跟随本轮）', 'input', 200, models);
    this.form.append(models);
    this.actions = element('div', 'ide-goal__actions');
    this.save = this.button('保存设置并暂停', () => {
      if (ACTIVE.has(this.data?.status) && !window.confirm('保存设置将暂停当前目标，之后恢复会启动新一轮执行。是否保存？')) return;
      void this.mutate('set');
    }, true);
    this.start = this.button('开始', () => { void this.mutate('start'); }, true);
    this.resume = this.button('恢复', () => { void this.mutate('resume'); }, true);
    this.pause = this.button('暂停', () => { void this.mutate('pause'); });
    this.clear = this.button('清除', () => {
      this.confirm.hidden = false;
      this.confirmCancel.focus();
    });
    this.reload = this.button('重新读取', () => { void this.load(); });
    this.discard = this.button('采用最新内容', () => {
      this.dirty = false;
      this.conflict = false;
      this.syncFields();
      this.render();
    });
    this.controls = element('div', 'ide-goal__actions');
    this.controls.append(this.resume, this.pause, this.edit, this.reload);
    this.actions.append(this.save, this.clear, this.discard);
    this.confirm = element('div', 'ide-goal__confirm');
    this.confirm.hidden = true;
    this.confirm.setAttribute('role', 'group');
    this.confirm.setAttribute('aria-label', '确认清除 Goal');
    this.confirm.append(element('p', 'ide-goal__hint', '清除将删除本 Goal 及其专属评审、证据记录，不影响 Todo。'));
    const confirmActions = element('div', 'ide-goal__actions');
    this.confirmYes = this.button('确认清除', () => { void this.mutate('clear'); });
    this.confirmCancel = this.button('取消', () => { this.confirm.hidden = true; this.clear.focus(); });
    confirmActions.append(this.confirmYes, this.confirmCancel);
    this.confirm.append(confirmActions);
    this.reviews = element('ol', 'ide-goal__reviews');
    this.reviews.setAttribute('aria-label', '评审记录');
    this.criteriaSection = element('section', 'ide-goal__section');
    this.criteriaSection.append(element('h3', 'ide-goal__section-title', '验收条件'), this.criteriaText);
    const reviewSection = element('section', 'ide-goal__section');
    this.reviewHeading = element('h3', 'ide-goal__section-title', '评审记录');
    this.emptyReviews = element('p', 'ide-goal__criteria', '尚无评审记录。');
    reviewSection.append(this.reviewHeading, this.emptyReviews, this.reviews);
    this.settings = element('details', 'ide-goal__section ide-goal__settings');
    this.settings.append(element('summary', '', '评审设置'), this.form, this.actions, this.confirm,
      element('p', 'ide-goal__hint', '保存设置会暂停目标。恢复时开启新一轮，已有计数不清零。'));
    this.body.append(this.compose, this.desktopHint, this.skeleton, this.goalText, this.meta, this.controls, this.criteriaSection, reviewSection, this.settings);
    this.ownerNotice = element('p', 'ide-goal__hint');
    this.ownerNotice.setAttribute('role', 'status');
    card.append(summary, this.ownerNotice, this.notice, this.body);
    this.root.append(card);
    ctx.el.append(this.style, this.root);
    this.root.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      if (!this.confirm.hidden) { this.confirm.hidden = true; this.clear.focus(); }
      else if (this.ctx.close) { this.ctx.close(); }
      else return;
      event.stopPropagation();
    });
    this.unsubscribe = ctx.events?.on('ide_goal_updated', payload => {
      if (!payload || payload.session_id !== (this.ownerSessionId || this.sessionId)) return;
      if (this.data && Number.isSafeInteger(payload.revision) && payload.revision <= this.data.revision) return;
      this.pendingRefresh = true;
      void this.load();
    });
    this.onVisibility = () => { if (!document.hidden) void this.load(); };
    document.addEventListener('visibilitychange', this.onVisibility);
    // ponytail: bounded polling covers reconnects; shared push invalidation avoids a separate stream.
    this.timer = window.setInterval(() => { if (!document.hidden) void this.load(); }, 4000);
    this.update(ctx);
    void this.syncDraft();
  },

  async syncDraft() {
    if (this.disposed || this.syncingDraft || !this.ctx.composer?.getDraft) return;
    this.syncingDraft = true;
    const version = this.draftVersion;
    const sessionId = this.sessionId;
    try {
      const snapshot = await this.ctx.composer.getDraft();
      if (this.disposed || sessionId !== this.sessionId || version !== this.draftVersion) return;
      this.draftSnapshot = snapshot;
      if (!this.draftError && snapshot) this.draft.value = snapshot.text;
      this.mobile = (this.ctx.data?.viewportWidth?.() ?? 1024) < 768;
      this.renderDraft();
    } catch (error) {
      if (!this.disposed && sessionId === this.sessionId && version === this.draftVersion) {
        this.draftError = error.message; this.renderDraft();
      }
    } finally { this.syncingDraft = false; }
  },

  renderDraft() {
    const snapshot = this.draftSnapshot;
    const active = ACTIVE.has(this.data?.status);
    this.compose.hidden = !this.mobile || !this.ctx.composer?.getDraft
      || (!this.editingGoal && (active || (!this.data && this.sessionId !== 'no-session')));
    this.desktopHint.hidden = Boolean(this.mobile);
    this.submitDraft.textContent = snapshot?.busy || this.submittingDraft ? '正在提交…'
      : !this.data || ['empty', 'completed'].includes(this.data.status) ? '创建并开始' : '修改并重新开始';
    this.retryDraft.hidden = !this.draftError;
    this.retryDraft.disabled = !this.data || this.submittingDraft || snapshot?.busy;
    this.draft.disabled = !snapshot || snapshot.busy || snapshot.mode !== 'ide.goal' || this.submittingDraft
      || this.isChild || Boolean(this.error) || (!this.data && this.sessionId !== 'no-session');
    this.submitDraft.disabled = this.draft.disabled || !this.draft.value.trim();
    const attachments = snapshot?.attachments?.map(item => item.name).join('、');
    this.draftNotice.textContent = this.draftError || (this.submittingDraft || snapshot?.busy ? '正在提交，请稍候。'
      : attachments ? `随目标提交的附件：${attachments}` : '关闭面板退出输入模式，草稿保留；不会暂停已开始的目标。');
  },

  async submitGoal() {
    if (this.submittingDraft || this.draft.disabled || !this.draft.value.trim()) return;
    const sessionId = this.sessionId;
    const current = () => !this.disposed && sessionId === this.sessionId;
    this.submittingDraft = true;
    this.renderDraft();
    try {
      await this.draftWrite;
      if (!current() || this.draftWriteFailed) return;
      this.draftError = '';
      if (await this.ctx.composer.submit() !== true) throw new Error('目标未提交，请核对输入状态后重试。');
      if (!current()) return;
      ++this.draftVersion;
      this.draftError = '';
      this.draft.value = '';
      this.editingGoal = false;
      await this.load();
    } catch (error) { if (current()) { this.draftError = error.message; await this.load(); } }
    finally {
      if (current()) {
        this.submittingDraft = false;
        await this.syncDraft(); this.renderDraft();
      }
    }
  },

  button(text, callback, primary = false) {
    const button = element('button', `ide-goal__button${primary ? ' ide-goal__button--primary' : ''}`, text);
    button.type = 'button';
    button.addEventListener('click', callback);
    return button;
  },

  field(text, tag, maxLength, parent = this.form) {
    const label = element('label', 'ide-goal__label');
    const field = element(tag, 'ide-goal__field');
    if (tag === 'input') field.type = 'text';
    field.maxLength = maxLength;
    field.value = '';
    field.addEventListener('input', () => {
      this.dirty = true;
      this.renderControls();
    });
    label.append(element('span', '', text), field);
    parent.append(label);
    return field;
  },

  update(ctx) {
    this.ctx = ctx;
    const sid = String(ctx.data?.sessionId || '');
    if (sid === this.sessionId) return;
    this.abort();
    this.sessionId = sid;
    this.ownerSessionId = '';
    this.isChild = false;
    ++this.draftVersion;
    this.draftSnapshot = null;
    this.draftError = '';
    this.editingGoal = false;
    this.submittingDraft = false;
    this.data = null;
    this.error = '';
    this.dirty = false;
    this.conflict = false;
    this.mutating = false;
    this.pendingRefresh = false;
    this.confirm.hidden = true;
    if (ctx.state.sessionId !== sid) {
      ctx.state.sessionId = sid;
      ctx.state.expanded = false;
    }
    this.syncFields();
    this.render();
    if (sid && sid !== 'no-session') void this.load();
  },

  abort() {
    ++this.seq;
    this.controller?.abort();
    this.controller = null;
    window.clearTimeout(this.timeout);
    this.timeout = null;
  },

  accept(data, expectedOwner = '') {
    const identityMatches = data && typeof data.session_id === 'string' && data.session_id
      && (expectedOwner ? data.session_id === expectedOwner
        : data.session_id === this.sessionId || data.requested_session_id === this.sessionId);
    if (!identityMatches || data.schema_version !== 1 || typeof data.is_child !== 'boolean'
      || !Number.isSafeInteger(data.revision) || data.revision < 0
      || !Object.hasOwn(LABELS, data.status) || typeof data.goal !== 'string'
      || typeof data.criteria !== 'string' || !Array.isArray(data.reviews)) {
      throw new Error('Goal 响应格式无效，请重新读取。');
    }
    if (this.ownerSessionId && data.session_id !== this.ownerSessionId) throw new Error('Goal 归属已变化，请重新打开面板核对。');
    if (this.data && data.revision < this.data.revision) throw new Error('收到旧版本 Goal，请重新读取。');
    if (this.dirty && this.data && this.data.revision !== data.revision) this.conflict = true;
    this.ownerSessionId = data.session_id;
    this.isChild = data.is_child;
    this.data = data;
    this.error = '';
    if (!this.dirty) this.syncFields();
    this.render();
  },

  syncFields() {
    this.goalText.textContent = this.data?.goal || '';
    this.criteria.value = this.data?.criteria || '';
    this.criteriaText.textContent = this.data?.criteria || '未单独设置，按目标要求验收。';
    this.provider.value = this.data?.reviewer_provider || '';
    this.model.value = this.data?.reviewer_model || '';
  },

  async load() {
    if (this.disposed || !this.sessionId || this.sessionId === 'no-session' || this.controller || this.mutating) return;
    const seq = ++this.seq;
    const controller = new AbortController();
    this.controller = controller;
    this.pendingRefresh = false;
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    this.timeout = timeout;
    try {
      const data = await this.ctx.api.request('/plugins/ide/goal?session_id=' + encodeURIComponent(this.sessionId), { signal: controller.signal });
      if (this.disposed || seq !== this.seq) return;
      this.accept(data);
    } catch (error) {
      if (this.disposed || seq !== this.seq) return;
      this.error = `Goal 读取失败：${error.message || String(error)}`;
      this.render();
    } finally {
      window.clearTimeout(timeout);
      if (this.controller === controller) {
        this.controller = null;
        this.timeout = null;
        if (this.pendingRefresh && !this.error) void this.load();
      }
    }
  },

  async mutate(action) {
    if (this.disposed || !this.data || this.isChild || this.data.is_child || this.mutating || this.error) return;
    if (action !== 'pause' && this.conflict) return;
    if (action === 'set' && !this.data.goal.trim()) return;
    if (action !== 'set' && action !== 'pause' && this.dirty) return;
    const ownerSessionId = this.ownerSessionId;
    const body = { action, expected_session_id: ownerSessionId, expected_revision: this.data.revision, request_id: requestId() };
    if (action === 'set') Object.assign(body, {
      goal: this.data.goal, criteria: this.criteria.value.trim(),
      reviewer_provider: this.provider.value.trim(), reviewer_model: this.model.value.trim(),
    });
    this.abort();
    const seq = this.seq;
    const controller = new AbortController();
    this.controller = controller;
    this.mutating = true;
    this.confirm.hidden = true;
    this.renderControls();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    this.timeout = timeout;
    let failed = false;
    try {
      const data = await this.ctx.api.request('/plugins/ide/goal?session_id=' + encodeURIComponent(ownerSessionId), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body), signal: controller.signal,
      });
      if (this.disposed || seq !== this.seq) return;
      // Validate identity before discarding the draft, including on clear responses.
      this.accept(data, ownerSessionId);
      if (action !== 'pause') {
        this.dirty = false;
        this.conflict = false;
        this.syncFields();
      }
    } catch (error) {
      if (this.disposed || seq !== this.seq) return;
      failed = true;
      this.conflict = true;
      this.error = `操作未确认成功：${error.message || String(error)}。正在重新读取，请勿重复提交。`;
    } finally {
      window.clearTimeout(timeout);
      if (seq === this.seq && !this.disposed) {
        this.controller = null;
        this.timeout = null;
        this.mutating = false;
        this.render();
        // A failed POST may have committed: read state, never retry the write automatically.
        if (failed || this.pendingRefresh) void this.load();
      }
    }
  },

  renderControls() {
    const unavailable = !this.data || this.isChild || Boolean(this.error) || this.mutating || this.conflict;
    const status = this.data?.status || 'empty';
    const active = ACTIVE.has(status);
    for (const field of [this.criteria, this.provider, this.model]) field.disabled = !this.data || this.isChild || this.mutating || Boolean(this.error);
    this.save.disabled = unavailable || !this.data?.goal;
    this.edit.disabled = unavailable || !this.ctx.composer;
    this.edit.textContent = this.data?.status === 'completed' ? '以此创建新目标' : '修改目标';
    this.start.hidden = status !== 'paused' || Boolean(this.data?.rounds) || Boolean(this.data?.reviews.length);
    this.resume.hidden = !['paused', 'blocked'].includes(status);
    this.pause.hidden = !active;
    this.clear.hidden = status === 'empty';
    for (const button of [this.start, this.resume, this.clear, this.confirmYes]) button.disabled = unavailable || this.dirty;
    // Unsaved edits must not prevent stopping automatic execution.
    this.pause.disabled = !this.data || this.isChild || Boolean(this.error) || this.mutating;
    this.reload.disabled = this.mutating;
    this.discard.hidden = !this.dirty && !this.conflict;
    this.discard.disabled = !this.data || this.mutating || Boolean(this.error);
    this.notice.hidden = !this.error && !this.conflict && !this.mutating;
    this.notice.textContent = this.error || (this.mutating ? '正在保存，请稍候。' : this.conflict
      ? '状态已更新或上次操作结果未确认。草稿已保留；请核对最新状态，复制需要的内容，再选择“采用最新内容”。' : '');
  },

  render() {
    this.root.hidden = !this.sessionId;
    this.body.hidden = this.isChild;
    this.ownerNotice.hidden = !this.isChild && (!this.ownerSessionId || this.ownerSessionId === this.sessionId);
    this.ownerNotice.textContent = this.isChild ? '当前为真实子会话，不能管理父会话 Goal。'
      : this.ownerSessionId && this.ownerSessionId !== this.sessionId
        ? `当前查看入口分支：${this.sessionId}\n此 Goal 属于主会话：${this.ownerSessionId}。以下操作将作用于该主会话。` : '';
    const status = this.data?.status || 'empty';
    this.root.dataset.status = status;
    this.title.textContent = '当前目标';
    this.goalText.textContent = this.data?.goal || (this.data || this.sessionId === 'no-session' ? '尚未设置目标。输入正文后开始。' : '');
    this.badge.textContent = this.error ? '读取未确认' : this.data ? LABELS[status] : this.sessionId === 'no-session' ? '未设置' : '读取中';
    this.settings.hidden = !this.data?.goal;
    this.criteriaSection.hidden = !this.data?.goal;
    this.edit.hidden = !this.data?.goal;
    this.reviewHeading.textContent = `评审记录${this.data?.reviews.length ? ' · ' + this.data.reviews.length : ''}`;
    this.emptyReviews.hidden = Boolean(this.data?.reviews.length);
    this.skeleton.hidden = Boolean(this.data) || Boolean(this.error) || this.sessionId === 'no-session';
    this.root.setAttribute('aria-busy', String(this.mutating || (!this.data && !this.error)));
    this.form.hidden = !this.data;
    this.meta.textContent = this.data
      ? `自动续轮 ${this.data.rounds ?? 0}/${this.data.max_rounds ?? 8} · 连续无进展 ${this.data.stagnation ?? 0}/3${this.data.pause_reason ? '\n' + this.data.pause_reason : ''}` : '';
    this.renderControls();
    this.renderDraft();
    const signature = this.data ? `${this.sessionId}:${this.data.revision}` : '';
    if (signature !== this.reviewSignature) {
      this.reviewSignature = signature;
      this.reviews.replaceChildren();
      // Backend bounds retention; all retained records remain available in this scroll region.
      for (const review of [...(this.data?.reviews || [])].reverse()) {
        const verdict = review.verdict || review;
        const title = verdict.passed === true ? '评审通过' : verdict.passed === false ? '评审未通过' : '评审记录';
        const applied = review.applied ? '已应用' : '尚未应用';
        const detail = verdict.feedback || review.error || review.status || '';
        const row = element('li', 'ide-goal__review');
        const index = this.data.reviews.indexOf(review) + 1;
        row.append(element('strong', 'ide-goal__review-title', `第 ${index} 次 · ${title}`), element('span', '', detail));
        if (!review.applied) row.append(element('p', 'ide-goal__hint', applied));
        this.reviews.append(row);
      }
    }
  },

  destroy() {
    this.disposed = true;
    this.abort();
    window.clearInterval(this.timer);
    window.clearInterval(this.draftTimer);
    document.removeEventListener('visibilitychange', this.onVisibility);
    this.unsubscribe?.();
    this.data = null;
    this.root.remove();
    this.style.remove();
  },
};

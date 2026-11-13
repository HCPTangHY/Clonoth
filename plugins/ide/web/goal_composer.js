/* Goal owns its submission policy; the host only binds the visible composer. */
export default {
  mount(ctx) {
    this.ctx = ctx;
    this.disposed = false;
    this.identity = null;
    this.identityError = '';
    this.identitySeq = 0;
    this.identitySession = null;
    this.store = ctx.composer?.getState?.('ide.goal') || ctx.state;
    this.store.pendingSubmissions ||= {};
    this.mode = { id: 'ide.goal', command: '/goal', label: '开始目标',
      placeholder: '描述目标、限制和完成条件… Enter 提交并开始',
      onSubmit: submission => this.submit(submission) };
    this.unregister = ctx.composer?.registerMode(this.mode);
    this.style = document.createElement('style');
    // Match the host's square Duties controls, not a separate rounded chip theme.
    this.style.textContent = `
.ide-goal-switch { display:inline-flex; align-items:center; justify-content:center; gap:4px; min-height:24px; padding:2px 8px; border:1px solid var(--duties-border,#ccc); border-radius:0; background:var(--duties-bg,#f1f0ee); color:var(--duties-secondary,#4a4a4a); font:inherit; font-family:'Geist Mono',ui-monospace,monospace; font-size:12px; line-height:18px; cursor:pointer; transition:background-color 150ms,border-color 150ms,color 150ms; }
.ide-goal-switch:hover:not(:disabled) { border-color:var(--duties-text,#252525); color:var(--duties-text,#252525); }
.ide-goal-switch[aria-pressed=true] { border-color:var(--duties-text,#252525); background:var(--duties-accent,#feaf2c); color:var(--duties-text,#252525); }
.ide-goal-switch:focus-visible { outline:2px solid var(--duties-text,#252525); outline-offset:1px; }
.ide-goal-switch:disabled { opacity:.5; cursor:default; }
.ide-goal-switch[hidden] { display:none; }
@media(prefers-reduced-motion:reduce) { .ide-goal-switch { transition:none; } }
`;
    this.button = document.createElement('button');
    this.button.type = 'button';
    this.button.className = 'ide-goal-switch';
    this.button.textContent = '◎ Goal';
    this.button.setAttribute('aria-label', '打开 Goal 面板');
    this.button.addEventListener('click', async () => {
      if (this.identityError) await this.loadIdentity();
      if (this.button.disabled || this.identityError || this.disposed) return;
      this.openingPanel = true;
      try {
        if (this.ctx.composer?.setMode('ide.goal', undefined, { focus: window.innerWidth >= 768 }) === false) return;
        await this.ctx.api.call('openPanel', 'plugin:ide:goal', { composerSessionId: this.ctx.data?.sessionId || 'no-session' });
      } catch (error) { this.message.textContent = error.message; this.message.hidden = false; }
      finally {
        this.openingPanel = false;
        if (!this.disposed) this.update(this.ctx);
      }
    });
    ctx.el.append(this.style, this.button);
    this.retry = document.createElement('button');
    this.retry.type = 'button';
    this.retry.className = 'ide-goal-switch';
    this.retry.textContent = '核对最新版本';
    this.retry.hidden = true;
    this.retry.addEventListener('click', () => {
      const sid = this.ctx.data?.sessionId;
      const pending = this.store.pendingSubmissions?.[sid];
      if (!pending || !window.confirm('请先核对 Goal 状态。重新提交将按最新状态新建或修改目标；若上个目标已完成，将重置预算和专属记录。是否重新选择本次操作？')) return;
      delete this.store.pendingSubmissions[sid];
      delete this.store.metadata;
      this.retry.hidden = true;
    });
    ctx.el.append(this.retry);
    this.message = document.createElement('span');
    this.message.setAttribute('role', 'status');
    this.message.style.cssText = 'font-size:12px;line-height:18px;color:var(--duties-secondary);';
    this.message.hidden = true;
    ctx.el.append(this.message);
    this.panelVisible = false;
    this.scope = '';
    this.onResize = () => this.update(this.ctx);
    window.addEventListener('resize', this.onResize);
    this.update(ctx);
  },
  acceptsIdentity(data, sessionId) {
    return data && typeof data.session_id === 'string' && data.session_id
      && (data.session_id === sessionId || data.requested_session_id === sessionId)
      && typeof data.is_child === 'boolean' && Number.isSafeInteger(data.revision) && data.revision >= 0
      && ['empty', 'completed', 'paused', 'running', 'waiting', 'reviewing', 'blocked'].includes(data.status);
  },
  async loadIdentity() {
    const sid = this.identitySession;
    if (!sid || sid === 'no-session' || this.disposed || this.identityController) return;
    const seq = ++this.identitySeq;
    const controller = new AbortController();
    this.identityController = controller;
    this.identityError = '';
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    this.identityTimeout = timeout;
    this.renderIdentity();
    try {
      const data = await this.ctx.api.request('/plugins/ide/goal?session_id=' + encodeURIComponent(sid), { signal: controller.signal });
      if (this.disposed || seq !== this.identitySeq) return;
      if (!this.acceptsIdentity(data, sid)) throw new Error('Goal 会话归属响应无效。');
      this.identity = data;
    } catch (error) {
      if (this.disposed || seq !== this.identitySeq) return;
      this.identityError = `Goal 身份读取失败：${error.message || String(error)}。点击 Goal 重新读取。`;
    } finally {
      window.clearTimeout(timeout);
      if (this.identityController === controller) {
        this.identityTimeout = null;
        this.identityController = null;
        if (!this.disposed) this.update(this.ctx);
      }
    }
  },
  renderIdentity() {
    const needsIdentity = Boolean(this.identitySession && this.identitySession !== 'no-session');
    const loading = needsIdentity && (Boolean(this.identityController) || (!this.identity && !this.identityError));
    const child = this.identity?.is_child === true;
    this.button.hidden = false;
    this.button.textContent = this.identityError ? '◎ Goal · 重试' : '◎ Goal';
    this.button.setAttribute('aria-busy', String(loading));
    this.button.title = this.identityError ? '加载失败，点击重试' : child ? '此会话不支持 Goal' : '打开 Goal 面板';
    this.button.disabled = !this.ctx.composer || this.ctx.composer.busy || loading || child;
    this.button.setAttribute('aria-pressed', String(this.ctx.composer?.mode === 'ide.goal'));
    // Identity checks stay internal; only an actionable failure needs visible feedback.
    this.message.textContent = '';
    this.message.hidden = true;
    this.retry.hidden = child || !this.store.pendingSubmissions?.[this.ctx.data?.sessionId]?.conflicted;
    this.retry.disabled = Boolean(this.ctx.composer?.busy) || loading;
  },
  update(ctx) {
    this.ctx = ctx;
    const scope = String(ctx.data?.composerScope || ctx.data?.conversationId || ctx.data?.sessionId || 'welcome');
    const sid = String(ctx.data?.sessionId || '');
    const changed = this.scope !== scope || this.identitySession !== sid;
    if (changed) {
      ++this.identitySeq;
      this.identityController?.abort();
      window.clearTimeout(this.identityTimeout);
      this.identityTimeout = null;
      this.identityController = null;
      this.identitySession = sid;
      this.identity = null;
      this.identityError = '';
      this.stripCommand = false;
    }
    this.store = ctx.composer?.getState?.('ide.goal') || ctx.state;
    this.store.pendingSubmissions ||= {};
    const allowed = !sid || sid === 'no-session' || (this.identity && !this.identity.is_child && !this.identityError);
    const visible = ctx.data?.panelId === 'plugin:ide:goal' && ctx.data?.rightPanelOpen === true && allowed;
    const wasVisible = this.scope === scope && this.panelVisible;
    this.scope = scope;
    this.panelVisible = visible;
    if (visible && (!wasVisible || (window.innerWidth >= 768 && !ctx.composer?.busy
        && ctx.composer?.mode !== 'ide.goal'))) {
      ctx.composer?.setMode('ide.goal', undefined, { focus: !wasVisible && window.innerWidth >= 768 });
    } else if (!visible && !this.openingPanel && (wasVisible || ctx.composer?.mode === 'ide.goal')) {
      if (ctx.composer?.mode === 'ide.goal') {
        const draft = ctx.composer.getDraft?.();
        if (draft && /^\s*\/goal(?:\s|$)/.test(draft.text)) {
          this.stripCommand = ctx.composer.setDraft(draft.text.replace(/^\s*\/goal(?:\s+|$)/, '')) === false;
        }
        ctx.composer.setMode('', undefined, { focus: false });
      }
      if (wasVisible && !ctx.data?.panelId && ctx.data?.rightPanelOpen && window.innerWidth < 768) {
        void ctx.api.call('setRightPanelOpen', false);
      }
    }
    if (this.stripCommand && !ctx.composer?.busy && ctx.composer?.mode === '') {
      const draft = ctx.composer.getDraft();
      if (/^\s*\/goal(?:\s|$)/.test(draft.text)) ctx.composer.setDraft(draft.text.replace(/^\s*\/goal(?:\s+|$)/, ''));
      this.stripCommand = false;
    }
    // Keep raw view identity in host intents; Goal ownership is plugin-owned.
    if (visible && (!wasVisible || this.notifiedSession !== ctx.data?.sessionId)) {
      this.notifiedSession = ctx.data?.sessionId;
      void ctx.api.call('openPanel', 'plugin:ide:goal', {
        composerSessionId: ctx.data?.sessionId || 'no-session', resetEditor: !wasVisible,
      });
    }
    if (!visible) this.notifiedSession = '';
    this.renderIdentity();
    if (changed && sid && sid !== 'no-session') void this.loadIdentity();
  },
  async submit({ text, attachments, sessionId, requestId, signal, state }) {
    if (this.disposed) throw new Error('Goal 插件已卸载，目标未提交。');
    const scope = this.scope;
    const originalView = String(this.ctx.data?.sessionId || '');
    const current = () => !this.disposed && !signal.aborted && this.scope === scope
      && (String(this.ctx.data?.sessionId || '') === originalView || ((!originalView || originalView === 'no-session') && this.ctx.data?.sessionId === sessionId));
    const assertCurrent = () => { if (!current()) throw new Error('会话已切换或提交已取消，正文已保留。'); };
    assertCurrent();
    if (originalView && originalView !== 'no-session' && originalView !== sessionId) throw new Error('Goal 输入会话已失效。');
    let goal = text.trim();
    if (attachments.length) {
      goal += '\n\n参考附件（以下路径相对于 Clonoth 安装根目录，而非当前项目工作区；使用可访问路径读取）：\n';
      goal += attachments.map(item => `- ${JSON.stringify(item.name)}：${JSON.stringify(item.path)}`).join('\n');
    }
    if (!goal || goal.length > 16000) throw new Error('目标正文与附件路径总长度须为 1 至 16000 字符。');
    const store = state || this.store;
    const pendingBySession = store.pendingSubmissions ||= {};
    if (store.metadata?.sessionId === sessionId && store.metadata.resetSubmission === true) {
      delete pendingBySession[sessionId];
      delete store.metadata.resetSubmission;
    }
    let pending = pendingBySession[sessionId];
    if (!pending || pending.sessionId !== sessionId || pending.goal !== goal) {
      const data = await this.ctx.api.request('/plugins/ide/goal?session_id=' + encodeURIComponent(sessionId), { signal });
      assertCurrent();
      if (!this.acceptsIdentity(data, sessionId)) throw new Error('Goal 会话、版本或状态无效。');
      if (data.is_child) throw new Error('当前为真实子会话，不能管理主会话 Goal。');
      const ownerSessionId = data.session_id;
      const baseline = store.metadata?.sessionId === sessionId ? store.metadata : null;
      if (baseline?.ownerSessionId && baseline.ownerSessionId !== ownerSessionId) throw new Error('Goal 归属已变化，请核对最新版本。');
      const revision = baseline?.revision ?? data.revision;
      const action = baseline?.action || (['empty', 'completed'].includes(data.status) ? 'create' : 'submit');
      if (action === 'create' && data.capabilities?.create !== true) {
        throw new Error('IDE 插件版本不支持新建 Goal，请先更新插件。');
      }
      if (action === 'create' && data.status === 'completed' && revision === data.revision
          && !window.confirm('创建新 Goal 将替换上个已完成目标，重置续轮预算、评审和专属记录。聊天历史及 Todo 保留。是否创建并开始？')) {
        throw new Error('已取消创建新 Goal，正文已保留。');
      }
      store.metadata = { ...baseline, sessionId, ownerSessionId, revision, action };
      pending = { sessionId, ownerSessionId, goal, revision, action, conflicted: revision !== data.revision,
        body: JSON.stringify({ action, goal, expected_session_id: ownerSessionId,
          expected_revision: revision, request_id: requestId }) };
      pendingBySession[sessionId] = pending;
    }
    if (pending.conflicted && !this.disposed && this.store === store) this.retry.hidden = false;
    if (pending.conflicted) throw new Error('Goal 已更新。请核对状态并选择“核对最新版本”后再提交，草稿已保留。');
    assertCurrent();
    try {
      const ownerSessionId = pending.ownerSessionId || sessionId;
      const result = await this.ctx.api.request('/plugins/ide/goal?session_id=' + encodeURIComponent(ownerSessionId), { method: 'POST', signal,
        headers: { 'Content-Type': 'application/json' }, body: pending.body });
      assertCurrent();
      if (result.session_id !== ownerSessionId || !Number.isSafeInteger(result.revision) || result.revision < pending.revision) throw new Error('Goal 提交未确认，请检查目标状态后重试。');
      if (pendingBySession[sessionId] === pending) delete pendingBySession[sessionId];
      delete store.metadata;
      if (!this.disposed && this.store === store) this.retry.hidden = true;
      return result;
    } catch (error) {
      if (/409/.test(String(error.message))) {
        pending.conflicted = true;
        if (!this.disposed && this.ctx.data?.sessionId === sessionId) this.retry.hidden = false;
      }
      throw error;
    }
  },
  destroy() {
    this.disposed = true;
    ++this.identitySeq;
    this.identityController?.abort();
    window.clearTimeout(this.identityTimeout);
    window.removeEventListener('resize', this.onResize);
    this.unregister?.();
    this.button.remove();
    this.retry.remove();
    this.message.remove();
    this.style.remove();
  },
};

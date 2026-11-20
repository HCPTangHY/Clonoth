// host.reasoning_effort — Web 内置会话思考强度控制（input_controls 槽位）。
// 通过 registerHostSlot 挂载至 PluginSlotHost，与 ide.goal_composer 同排展示，
// 仅作用于当前会话（欢迎页未创建会话时同步 pendingProviderOverride）。

const PSYCHOLOGY_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" width="14" height="14" fill="currentColor" aria-hidden="true">' +
  '<path d="M320-120v-170q-57-52-88.5-121.5T200-560q0-150 105-255t255-105q125 0 221.5 73.5T899-659l52 205q5 19-7 34.5T913-404h-73v124q0 33-23.5 56.5T760-200h-80v80h-80v-160h160v-200h108l-38-155q-23-91-98-148t-172-57q-116 0-198 81t-82 197q0 60 24.5 114t69.5 96l26 24v208h-80Zm214-360Zm-54 80h80l6-50q8-3 14.5-7t11.5-9l46 20 40-68-40-30q2-8 2-16t-2-16l40-30-40-68-46 20q-5-5-11.5-9t-14.5-7l-6-50h-80l-6 50q-8 3-14.5 7t-11.5 9l-46-20-40 68 40 30q-2 8-2 16t2 16l-40 30 40 68 46-20q5 5 11.5 9t14.5 7l6 50Zm40-100q-25 0-42.5-17.5T460-560q0-25 17.5-42.5T520-620q25 0 42.5 17.5T580-560q0 25-17.5 42.5T520-500Z"/>' +
  '</svg>';

const CHECK_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" width="14" height="14" fill="currentColor" aria-hidden="true">' +
  '<path d="M382-240 154-468l57-57 171 171 367-367 57 57-424 424Z"/>' +
  '</svg>';

const EFFORT_OPTIONS = [
  { value: '', label: '默认', sublabel: '继承节点/渠道' },
  { value: 'none', label: '关闭', sublabel: 'none' },
  { value: 'low', label: '低', sublabel: 'low' },
  { value: 'medium', label: '中', sublabel: 'medium' },
  { value: 'high', label: '高', sublabel: 'high' },
  { value: 'xhigh', label: '极高', sublabel: 'xhigh' },
];

const CSS = `
.host-effort-wrap { position:relative; display:inline-flex; align-items:center; gap:4px; }
.host-effort-btn {
  display:inline-flex; align-items:center; justify-content:center; gap:4px;
  min-height:24px; padding:2px 8px;
  border:1px solid var(--duties-border,#ccc); border-radius:0;
  background:var(--duties-bg,#f1f0ee); color:var(--duties-secondary,#4a4a4a);
  font:inherit; font-family:'Geist Mono',ui-monospace,monospace; font-size:12px; line-height:18px;
  cursor:pointer; white-space:nowrap;
  transition:background-color 150ms,border-color 150ms,color 150ms,transform 150ms;
}
.host-effort-btn:hover:not(:disabled) { border-color:var(--duties-text,#252525); color:var(--duties-text,#252525); }
.host-effort-btn:active:not(:disabled) { transform:scale(0.98); }
.host-effort-btn[data-overridden="1"] {
  border-color:var(--duties-text,#252525);
  background:var(--duties-accent,#feaf2c);
  color:var(--duties-text,#252525);
}
.host-effort-btn:focus-visible { outline:2px solid var(--duties-text,#252525); outline-offset:1px; }
.host-effort-btn:disabled { opacity:.5; cursor:default; }
.host-effort-pop {
  position:absolute; bottom:calc(100% + 4px); left:0; z-index:30;
  min-width:176px; padding:4px;
  border:1px solid var(--duties-border,#ccc);
  background:var(--duties-bg,#f1f0ee);
  box-shadow:0 4px 16px rgba(0,0,0,0.12);
}
.host-effort-head {
  padding:4px 8px; margin-bottom:2px;
  border-bottom:1px solid var(--duties-border,#ccc);
  font-family:'Geist Mono',ui-monospace,monospace; font-size:11px;
  color:var(--duties-tertiary,#6e6e73);
}
.host-effort-opt {
  display:flex; width:100%; align-items:center; justify-content:space-between; gap:12px;
  padding:5px 8px; border:0; background:transparent;
  font-family:'Geist Mono',ui-monospace,monospace; font-size:12px; line-height:18px;
  color:var(--duties-secondary,#4a4a4a); text-align:left; cursor:pointer;
}
.host-effort-opt:hover { background:var(--duties-panel,#e6e4e0); color:var(--duties-text,#252525); }
.host-effort-opt[aria-selected="true"] { font-weight:500; color:var(--duties-text,#252525); }
.host-effort-sub { display:inline-flex; align-items:center; gap:4px; color:var(--duties-tertiary,#6e6e73); font-size:11px; }
.host-effort-err { font-family:'Geist Mono',ui-monospace,monospace; font-size:11px; color:var(--duties-secondary,#4a4a4a); }
@media(prefers-reduced-motion:reduce) { .host-effort-btn { transition:none; } }
`;

function extractEffort(override) {
  if (!override || typeof override !== 'object') return '';
  if (typeof override.reasoning_effort === 'string' && override.reasoning_effort.trim()) {
    return override.reasoning_effort.trim().toLowerCase();
  }
  const po = override.provider_options;
  if (po && typeof po === 'object' && typeof po.reasoning_effort === 'string' && po.reasoning_effort.trim()) {
    return po.reasoning_effort.trim().toLowerCase();
  }
  return '';
}

export default {
  mount(ctx) {
    this.ctx = ctx;
    this.disposed = false;
    this.seq = 0;
    this.loadedSession = null;
    this.effort = '';
    this.open = false;
    this.saving = false;
    this.error = '';

    const style = document.createElement('style');
    style.textContent = CSS;

    this.wrap = document.createElement('div');
    this.wrap.className = 'host-effort-wrap';

    this.btn = document.createElement('button');
    this.btn.type = 'button';
    this.btn.className = 'host-effort-btn';
    this.btn.setAttribute('aria-haspopup', 'listbox');
    this.btn.addEventListener('click', () => {
      if (this.saving) return;
      this.open = !this.open;
      this.render();
    });

    this.labelEl = document.createElement('span');
    this.iconWrap = document.createElement('span');
    this.iconWrap.style.display = 'inline-flex';
    this.iconWrap.innerHTML = PSYCHOLOGY_SVG;
    this.btn.append(this.iconWrap, this.labelEl);

    this.errEl = document.createElement('span');
    this.errEl.className = 'host-effort-err';
    this.errEl.setAttribute('role', 'alert');
    this.errEl.hidden = true;

    this.pop = document.createElement('div');
    this.pop.className = 'host-effort-pop';
    this.pop.setAttribute('role', 'listbox');
    this.pop.setAttribute('aria-label', '选择本会话思考强度');
    this.pop.hidden = true;

    this.wrap.append(this.btn, this.errEl, this.pop);
    ctx.el.append(style, this.wrap);

    this.onDocPointer = (e) => {
      if (!this.open || !this.wrap) return;
      if (!this.wrap.contains(e.target)) {
        this.open = false;
        this.render();
      }
    };
    this.onDocKey = (e) => {
      if (this.open && e.key === 'Escape') {
        this.open = false;
        this.render();
      }
    };
    document.addEventListener('mousedown', this.onDocPointer);
    document.addEventListener('touchstart', this.onDocPointer);
    document.addEventListener('keydown', this.onDocKey);

    ctx.events.on('session_provider_override_changed', (payload) => {
      if (this.disposed || !payload || typeof payload !== 'object') return;
      const sid = this.ctx?.data?.sessionId || '';
      const target = sid ? payload.sessionProviderOverride : (payload.pendingProviderOverride ?? payload.sessionProviderOverride);
      this.effort = extractEffort(target);
      this.render();
    });

    this.syncSession(ctx.data?.sessionId || '');
    this.render();
  },

  update(ctx) {
    this.ctx = ctx;
    const sid = ctx.data?.sessionId || '';
    if (sid !== this.loadedSession) {
      this.open = false;
      this.syncSession(sid);
    }
    this.render();
  },

  async syncSession(sid) {
    this.loadedSession = sid;
    this.error = '';
    const seq = ++this.seq;
    try {
      const override = await this.ctx.api?.call?.('getSessionProviderOverride', sid);
      if (this.disposed || seq !== this.seq) return;
      this.effort = extractEffort(override);
      this.render();
    } catch {
      if (this.disposed || seq !== this.seq) return;
      this.effort = '';
      this.render();
    }
  },

  async selectEffort(nextVal) {
    this.open = false;
    this.error = '';
    if (nextVal === this.effort) {
      this.render();
      return;
    }
    const sid = this.ctx?.data?.sessionId || '';
    this.saving = true;
    this.render();
    try {
      const saved = await this.ctx.api?.call?.('patchSessionProviderOverride', sid, {
        reasoning_effort: nextVal || null,
      });
      if (this.disposed) return;
      this.effort = extractEffort(saved);
    } catch (err) {
      if (this.disposed) return;
      this.error = (err && err.message) ? err.message : '保存失败';
    } finally {
      if (!this.disposed) {
        this.saving = false;
        this.render();
      }
    }
  },

  render() {
    if (!this.btn) return;
    const sid = this.ctx?.data?.sessionId || '';
    const matched = EFFORT_OPTIONS.find((o) => o.value === this.effort);
    const displayLabel = matched ? matched.label : (this.effort || '默认');
    const overridden = Boolean(this.effort);

    this.labelEl.textContent = '思考 · ' + (this.saving ? '保存中' : displayLabel);
    this.btn.disabled = this.saving;
    this.btn.dataset.overridden = overridden ? '1' : '0';
    this.btn.setAttribute('aria-expanded', String(this.open));
    this.btn.setAttribute('aria-label', '当前会话思考强度：' + displayLabel + '。点击选择');
    this.btn.title = (sid ? '当前会话' : '新会话') + '思考强度：' + displayLabel;

    this.errEl.textContent = this.error;
    this.errEl.hidden = !this.error;

    this.pop.hidden = !this.open;
    if (this.open) {
      this.pop.innerHTML = '';
      const head = document.createElement('div');
      head.className = 'host-effort-head';
      head.textContent = '本会话思考强度';
      this.pop.appendChild(head);

      for (const opt of EFFORT_OPTIONS) {
        const selected = opt.value === this.effort;
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'host-effort-opt';
        row.setAttribute('role', 'option');
        row.setAttribute('aria-selected', String(selected));

        const titleSpan = document.createElement('span');
        titleSpan.textContent = opt.label;

        const subSpan = document.createElement('span');
        subSpan.className = 'host-effort-sub';
        const codeSpan = document.createElement('span');
        codeSpan.textContent = opt.sublabel;
        subSpan.appendChild(codeSpan);
        if (selected) {
          const checkSpan = document.createElement('span');
          checkSpan.style.display = 'inline-flex';
          checkSpan.innerHTML = CHECK_SVG;
          subSpan.appendChild(checkSpan);
        }

        row.append(titleSpan, subSpan);
        row.addEventListener('click', () => { void this.selectEffort(opt.value); });
        this.pop.appendChild(row);
      }
    }
  },

  destroy() {
    this.disposed = true;
    if (this.onDocPointer) {
      document.removeEventListener('mousedown', this.onDocPointer);
      document.removeEventListener('touchstart', this.onDocPointer);
    }
    if (this.onDocKey) {
      document.removeEventListener('keydown', this.onDocKey);
    }
  },
};

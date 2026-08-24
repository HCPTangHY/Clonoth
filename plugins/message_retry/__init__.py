"""消息重试/编辑：用户消息的原样重试与编辑后重试。

迁移自 MessageCard 内联实现（2026-08-23 前的 useUserRetryEdit hook）。
声明 message_footer 槽位，在每条消息卡片底部渲染操作按钮；编辑状态与
草稿保存在 ctx.state，宿主重挂载（切换会话/视图）后草稿不丢。

宿主侧依赖：
- MessageCard 的 message_footer 槽位 data：messageId/role/sessionId/
  retryable/text/isLastUserMessage
- ctx.api.call('retryMessage', messageId, newText?)（hostActions.ts 注册表，
  内部走 chatStore.retryMessage：凭证解析 + 后端重试 + UI 截断）
- article 的 group/card 类名（悬停显示样式挂在这个宿主类上）

行为与内联版一致：confirm 确认、Ctrl+Enter 提交、Esc 取消。
[2026-08-24] 可见性改为半透明常显 + 悬停全亮（原先桌面端完全不可见，
无悬停时是一块空白）。
"""

_RETRY_SCRIPT = r"""
export default {
  mount(ctx) {
    // ctx.state 按 slotId 共享于所有消息卡片，必须按 messageId 分桶，
    // 否则在 A 消息上开始编辑会同时展开 B 消息的编辑框。
    const state = ctx.state;
    state.msgs = state.msgs || {};

    const wrap = document.createElement('div');
    wrap.className = 'msg-retry-acts';
    // [AutoC 2026-08-24] 图标与宿主复制按钮完全同体系：Material Symbols
    // w400 path、fill=currentColor、13px，视觉尺寸/描边/基线一致。
    wrap.innerHTML =
      '<button type="button" class="mr-retry" title="原样重试"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" width="13" height="13" fill="currentColor" aria-hidden="true"><path d="M480-160q-133 0-226.5-93.5T160-480q0-133 93.5-226.5T480-800q85 0 149 34.5T740-671v-99q0-13 8.5-21.5T770-800q13 0 21.5 8.5T800-770v194q0 13-8.5 21.5T770-546H576q-13 0-21.5-8.5T546-576q0-13 8.5-21.5T576-606h138q-38-60-97-97t-137-37q-109 0-184.5 75.5T220-480q0 109 75.5 184.5T480-220q75 0 140-39.5T717-366q5-11 16.5-16.5t22.5-.5q12 5 16 16.5t-1 23.5q-39 84-117.5 133.5T480-160Z"/></svg></button>' +
      '<button type="button" class="mr-edit" title="编辑后重试"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" width="13" height="13" fill="currentColor" aria-hidden="true"><path d="M180-180h44l472-471-44-44-472 471v44Zm-30 60q-13 0-21.5-8.5T120-150v-73q0-12 5-23.5t13-19.5l557-556q8-8 19-12.5t23-4.5q11 0 22 4.5t20 12.5l44 44q9 9 13 20t4 22q0 11-4.5 22.5T823-694L266-138q-8 8-19.5 13t-23.5 5h-73Zm629-617-41-41 41 41Zm-105 64-22-22 44 44-22-22Z"/></svg></button>';

    const editor = document.createElement('div');
    editor.className = 'msg-retry-editor';
    editor.style.display = 'none';
    editor.innerHTML =
      '<textarea class="mr-textarea" rows="3"></textarea>' +
      '<div class="mr-buttons">' +
      '<button type="button" class="mr-submit">✓ 提交</button>' +
      '<button type="button" class="mr-cancel">✕ 取消</button>' +
      '</div>';

    this._ctx = ctx;
    this._wrap = wrap;
    this._editor = editor;
    this._ta = editor.querySelector('.mr-textarea');

    wrap.querySelector('.mr-retry').addEventListener('click', () => {
      console.debug('[msg_retry] retry clicked', ctx.data?.messageId);
      this._doRetry();
    });
    wrap.querySelector('.mr-edit').addEventListener('click', () => {
      console.debug('[msg_retry] edit clicked', ctx.data?.messageId);
      this._startEdit();
    });
    editor.querySelector('.mr-submit').addEventListener('click', () => this._submitEdit());
    editor.querySelector('.mr-cancel').addEventListener('click', () => this._cancelEdit());
    // 用户输入即草稿；宿主 update 永不覆盖正在编辑的内容
    this._ta.addEventListener('input', () => { this._msgState().draft = this._ta.value; });
    this._ta.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') this._cancelEdit();
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) this._submitEdit();
    });

    console.debug('[msg_retry] mounted', ctx.slotId, ctx.data?.messageId);
    ctx.el.appendChild(wrap);
    ctx.el.appendChild(editor);

    // 宿主重挂载（切换会话/视图）后恢复编辑状态与草稿
    const ms = this._msgState();
    if (ms.editing) {
      this._ta.value = ms.draft !== null ? ms.draft : String(ctx.data?.text || '');
    }
    this._apply(ctx);
  },
  update(ctx) {
    this._ctx = ctx;
    this._apply(ctx);
  },
  _msgState() {
    const key = String(this._ctx.data?.messageId || 'unknown');
    const bucket = this._ctx.state.msgs;
    if (!bucket[key]) bucket[key] = { editing: false, draft: null };
    return bucket[key];
  },
  _apply(ctx) {
    const retryable = !!ctx.data?.retryable;
    const ms = this._msgState();
    const editing = retryable && ms.editing;
    this._wrap.style.display = retryable && !editing ? '' : 'none';
    this._editor.style.display = editing ? '' : 'none';
  },
  _doRetry(newText) {
    const ctx = this._ctx;
    if (!window.confirm('将取消当前任务并截断此消息之后的所有内容，确认？')) return;
    const ms = this._msgState();
    ms.editing = false;
    ms.draft = null;
    void ctx.api?.call?.('retryMessage', ctx.data?.messageId, newText);
  },
  _startEdit() {
    const ctx = this._ctx;
    if (!ctx.data?.retryable) return;
    const ms = this._msgState();
    ms.editing = true;
    ms.draft = null;
    this._ta.value = String(ctx.data?.text || '');
    this._apply(ctx);
    this._ta.focus();
    this._ta.setSelectionRange(this._ta.value.length, this._ta.value.length);
  },
  _cancelEdit() {
    const ms = this._msgState();
    ms.editing = false;
    ms.draft = null;
    this._apply(this._ctx);
  },
  _submitEdit() {
    const text = this._ta.value.trim();
    this._doRetry(text || undefined);
  },
  destroy() {
    this._ctx = null;
    this._wrap = null;
    this._editor = null;
    this._ta = null;
  },
};
"""

_STYLES = r"""
/* [AutoC 2026-08-24] 透明度由父容器 .msg-footer-row 统一控制，本组件不再
   自行设置（否则与父容器叠加成 0.12，比重制按钮暗得多）。间距与对齐跟
   随父容器（gap 2px、垂直居中），悬停色跟随宿主正文色。 */
.msg-retry-acts {
  display: flex;
  align-items: center;
  gap: 2px;
}
/* [AutoC 2026-08-24] 尺寸对齐宿主复制按钮：SVG 图标 13px、内边距 2px。 */
.msg-retry-acts button {
  background: none;
  border: none;
  padding: 2px;
  cursor: pointer;
  color: var(--duties-tertiary);
  transition: color 0.15s ease;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.msg-retry-acts button svg { display: block; }
.msg-retry-acts button:hover { color: var(--duties-text); }
.msg-retry-editor {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.msg-retry-editor .mr-textarea {
  width: 100%;
  border: 1px solid var(--duties-border);
  background: var(--duties-bg);
  color: var(--duties-text);
  padding: 6px 8px;
  font-size: 12px;
  font-family: inherit;
  resize: vertical;
  min-height: 3rem;
  outline: none;
}
.msg-retry-editor .mr-textarea:focus {
  box-shadow: 0 0 0 1px #3b82f6;
}
.msg-retry-editor .mr-buttons {
  display: flex;
  gap: 8px;
}
.msg-retry-editor .mr-buttons button {
  font-family: var(--duties-mono, 'Geist Mono', ui-monospace, Menlo, monospace);
  font-size: 0.6rem;
  background: none;
  border: none;
  padding: 0 2px;
  cursor: pointer;
  transition: color 0.15s ease;
}
.msg-retry-editor .mr-submit { color: #2563eb; }
.msg-retry-editor .mr-submit:hover { color: #1d4ed8; }
.msg-retry-editor .mr-cancel { color: var(--duties-tertiary); }
.msg-retry-editor .mr-cancel:hover { color: #ef4444; }
"""

PLUGIN_META = {
    "name": "message_retry",
    "version": "1.4.0",
    "description": "消息重试/编辑：用户消息的原样重试与编辑后重试（message_footer 槽位）",
    "author": "clonoth",
    "client": {
        "slots": [
            {
                "slot_id": "message_retry.footer",
                "slot": "message_footer",
                "priority": 100,
                "mode": "append",
                "script": _RETRY_SCRIPT,
            }
        ],
        "styles": _STYLES,
    },
}


def register(ctx) -> None:
    """纯前端贡献插件：无后端注册动作。重试后端由 retry_api 内置插件提供。"""
    return None

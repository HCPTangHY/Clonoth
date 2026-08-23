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

行为与内联版一致：confirm 确认、Ctrl+Enter 提交、Esc 取消、桌面端
悬停显示/移动端常显。
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
    wrap.innerHTML =
      '<button type="button" class="mr-retry" title="原样重试">↻</button>' +
      '<button type="button" class="mr-edit" title="编辑后重试">✎</button>';

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

    wrap.querySelector('.mr-retry').addEventListener('click', () => this._doRetry());
    wrap.querySelector('.mr-edit').addEventListener('click', () => this._startEdit());
    editor.querySelector('.mr-submit').addEventListener('click', () => this._submitEdit());
    editor.querySelector('.mr-cancel').addEventListener('click', () => this._cancelEdit());
    // 用户输入即草稿；宿主 update 永不覆盖正在编辑的内容
    this._ta.addEventListener('input', () => { this._msgState().draft = this._ta.value; });
    this._ta.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') this._cancelEdit();
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) this._submitEdit();
    });

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
.msg-retry-acts {
  display: flex;
  gap: 6px;
  margin-top: 4px;
}
@media (min-width: 640px) {
  .msg-retry-acts { opacity: 0; transition: opacity 0.15s ease; }
  .group\/card:hover .msg-retry-acts { opacity: 1; }
}
.msg-retry-acts button {
  font-family: var(--duties-mono, 'Geist Mono', ui-monospace, Menlo, monospace);
  font-size: 0.55rem;
  background: none;
  border: none;
  padding: 0 2px;
  cursor: pointer;
  color: var(--duties-tertiary);
  transition: color 0.15s ease;
}
.msg-retry-acts button:hover { color: #2563eb; }
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
    "version": "1.0.0",
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

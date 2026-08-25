"""快捷重 roll 按钮：输入栏右对齐工具槽位的实用插件。

点击后撤回活动会话最后一条用户消息并重新提交（retry 语义）。
无后端动作：不注册 hook、不注册路由，纯前端贡献。
宿主侧依赖：input_toolbar_right 槽位 + ctx.api.call('reroll')（hostActions.ts 注册表）。

[AutoC 2026-08-22] 槽位上下文 v2 演示：
- ctx.state：本页重 roll 计数。宿主视图切换导致组件重挂载时计数不丢，
  只有插件卸载（贡献消失）时才被清除。
- ctx.events：订阅 task_started / task_completed 事件驱动按钮闪烁提示，
  不依赖宿主向 data 传 isGenerating。事件 payload 是 task 快照
  （supervisor/task_store.py _event_task_snapshot），带 session_id 字段。
"""

_ICON_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
    'aria-hidden="true">'
    '<path d="M21 12a9 9 0 1 1-2.64-6.36" />'
    '<path d="M21 3v6h-6" />'
    '</svg>'
)

_REROLL_SCRIPT = """
export default {
  mount(ctx) {
    const state = ctx.state;
    state.count = state.count || 0;

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'clonoth-reroll-btn';
    btn.innerHTML = '__ICON_SVG__';
    btn.addEventListener('click', () => {
      if (btn.disabled) return;
      state.count = (state.count || 0) + 1;
      this._applyTitle();
      void ctx.api?.call?.('reroll');
    });
    this._btn = btn;
    this._ctx = ctx;

    // 事件订阅：任务开始/结束时闪烁，展示与宿主 data 无关的反应能力。
    // 订阅由宿主在销毁时自动撤销，插件无需自行解绑。
    const inSession = (payload) => {
      const sid = ctx.data && ctx.data.sessionId;
      if (sid && payload && payload.session_id && payload.session_id !== sid) return false;
      return true;
    };
    this._flashTimer = null;
    const flash = () => {
      btn.classList.add('clonoth-reroll-flash');
      if (this._flashTimer) clearTimeout(this._flashTimer);
      this._flashTimer = setTimeout(() => btn.classList.remove('clonoth-reroll-flash'), 600);
    };
    ctx.events.on('task_started', (payload) => { if (inSession(payload)) flash(); });
    ctx.events.on('task_completed', (payload) => { if (inSession(payload)) flash(); });

    ctx.el.appendChild(btn);
    this._apply(ctx);
  },
  update(ctx) {
    this._ctx = ctx;
    this._apply(ctx);
  },
  _apply(ctx) {
    const disabled = !ctx.data?.rerollTargetId || !!ctx.data?.isGenerating;
    this._btn.disabled = disabled;
    this._applyTitle();
  },
  _applyTitle() {
    if (!this._btn) return;
    const n = this._ctx?.state?.count || 0;
    this._btn.title = n > 0
      ? '重新生成（本页已重 roll ' + n + ' 次）'
      : '重新生成：撤回到上一条用户消息并重发';
  },
  destroy() {
    if (this._flashTimer) clearTimeout(this._flashTimer);
    this._btn = null;
    this._ctx = null;
  },
};
"""

_STYLES = """
.clonoth-reroll-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 32px;
  width: 32px;
  border: 1px solid var(--duties-border);
  background: transparent;
  color: var(--duties-secondary);
  cursor: pointer;
  transition: border-color 0.15s ease, color 0.15s ease;
}
.clonoth-reroll-btn:hover:not(:disabled) {
  border-color: var(--duties-text);
  color: var(--duties-text);
}
.clonoth-reroll-btn:disabled {
  opacity: 0.3;
  cursor: not-allowed;
}
.clonoth-reroll-btn svg {
  width: 16px;
  height: 16px;
  display: block;
}
.clonoth-reroll-btn.clonoth-reroll-flash {
  border-color: var(--duties-text);
  color: var(--duties-text);
}
"""

PLUGIN_META = {
    "name": "reroll_button",
    "version": "1.1.0",
    "description": "输入栏快捷重 roll 按钮（撤回上一条用户消息并重发）",
    "author": "clonoth",
    "web": {
        "slots": [
            {
                "slot_id": "reroll_button.toolbar_right",
                "slot": "input_toolbar_right",
                "priority": 50,
                "mode": "append",
                "script": _REROLL_SCRIPT.replace("__ICON_SVG__", _ICON_SVG),
            }
        ],
        "styles": _STYLES,
    },
}


def register(ctx) -> None:
    """纯前端贡献插件：无后端注册动作。"""
    return None

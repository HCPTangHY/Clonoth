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

# [AutoC 2026-08-26] 图标换成 Material Symbols fill 版 refresh（与宿主审批按钮、
# message_retry 重试按钮同一族），viewBox 0 -960 960 960、fill currentColor，
# 尺寸由 CSS 控制 16px。
_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" '
    'fill="currentColor" aria-hidden="true">'
    '<path d="M480-160q-133 0-226.5-93.5T160-480q0-133 93.5-226.5T480-800q85 0 149 34.5T740-671v-99q0-13 8.5-21.5T770-800q13 0 21.5 8.5T800-770v194q0 13-8.5 21.5T770-546H576q-13 0-21.5-8.5T546-576q0-13 8.5-21.5T576-606h138q-38-60-97-97t-137-37q-109 0-184.5 75.5T220-480q0 109 75.5 184.5T480-220q75 0 140-39.5T717-366q5-11 16.5-16.5t22.5-.5q12 5 16 16.5t-1 23.5q-39 84-117.5 133.5T480-160Z"/>'
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
/* [AutoC 2026-08-26] 尺寸与配色对齐宿主工具栏按钮（审批级别切换钮）的公式：
   h-8 + 1px border + px-2 + --duties-secondary，悬停色 --duties-text。
   图标 Material Symbols fill 16px，与宿主 Icon size={16} 一致。 */
.clonoth-reroll-btn {
  display: inline-flex;
  height: 32px;
  align-items: center;
  justify-content: center;
  padding: 0 8px;
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
    "version": "1.2.0",
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

"""快捷重 roll 按钮：输入栏右对齐工具槽位的实用插件。

点击后撤回活动会话最后一条用户消息并重新提交（retry 语义）。
无后端动作：不注册 hook、不注册路由，纯前端贡献。
宿主侧依赖：input_toolbar_right 槽位 + ctx.api.reroll（见 slotApi.ts）。
"""

_ICON_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
    'aria-hidden="true">'
    '<path d="M21 12a9 9 0 1 1-2.64-6.36" />'
    '<path d="M21 3v6h-6" />'
    '</svg>'
)

_REROLL_SCRIPT = f"""
export default {{
  mount(ctx) {{
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'clonoth-reroll-btn';
    btn.title = '重新生成：撤回到上一条用户消息并重发';
    btn.innerHTML = {_ICON_SVG!r};
    btn.addEventListener('click', () => {{
      if (btn.disabled) return;
      void ctx.api?.reroll?.();
    }});
    this._btn = btn;
    ctx.el.appendChild(btn);
    this._apply(ctx);
  }},
  update(ctx) {{ this._apply(ctx); }},
  _apply(ctx) {{
    const disabled = !ctx.data?.rerollTargetId || !!ctx.data?.isGenerating;
    this._btn.disabled = disabled;
  }},
  destroy() {{ this._btn = null; }},
}};
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
"""

PLUGIN_META = {
    "name": "reroll_button",
    "version": "1.0.0",
    "description": "输入栏快捷重 roll 按钮（撤回上一条用户消息并重发）",
    "author": "clonoth",
    "client": {
        "slots": [
            {
                "slot_id": "reroll_button.toolbar_right",
                "slot": "input_toolbar_right",
                "priority": 50,
                "script": _REROLL_SCRIPT,
            }
        ],
        "styles": _STYLES,
    },
}


def register(ctx) -> None:
    """纯前端贡献插件：无后端注册动作。"""
    return None

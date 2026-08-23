"""演示插件：验证插件前端贡献三级机制（panel + slot + styles）。

声明一个右侧面板（iframe 加载本目录 client/index.html）、一个会话条目
徽章槽位贡献、一段纯 CSS。同时用 routes face 把 client/ 目录以静态文件
形式挂到 /v1/plugins/demo_panel/client/（public，iframe 无 Authorization
头也能加载）。engine 进程没有 routes face，register() 直接跳过。
"""

from pathlib import Path

PLUGIN_META = {
    "name": "demo_panel",
    "version": "1.0.0",
    "description": "演示插件：panel + slot + styles 三级前端贡献",
    "author": "clonoth",
    "client": {
        "panels": [
            {
                "id": "demo",
                "slot": "right",
                "title": "演示面板",
                "entry": "/v1/plugins/demo_panel/client/",
            }
        ],
        "slots": [
            {
                "slot_id": "demo_panel.session_badge",
                "slot": "session_item_suffix",
                "priority": 50,
                "script": (
                    "export default function mount(ctx) {\n"
                    "  const el = document.createElement('span');\n"
                    "  el.className = 'demo-plugin-badge';\n"
                    "  el.textContent = 'demo';\n"
                    "  el.title = 'demo_panel 插件槽位贡献';\n"
                    "  ctx.el.appendChild(el);\n"
                    "}\n"
                ),
            }
        ],
        "styles": (
            ".demo-plugin-badge {\n"
            "  display: inline-block;\n"
            "  margin-left: 6px;\n"
            "  padding: 0 4px;\n"
            "  border: 1px solid #a855f7;\n"
            "  color: #a855f7;\n"
            "  border-radius: 3px;\n"
            "  font-size: 9px;\n"
            "  font-family: ui-monospace, monospace;\n"
            "  line-height: 14px;\n"
            "}\n"
        ),
    },
}


def register(ctx) -> None:
    """挂载静态面板资源。engine 进程没有 routes face，直接跳过。"""
    routes = getattr(ctx.contributions, "get", lambda _name: None)("routes")
    if routes is None:
        return
    from fastapi import APIRouter

    from engine.faces.routes import static_router

    # face 把 router 直接挂在 /v1/plugins/{owner} 下；面板资源要出现在
    # /client/ 子路径，用 include_router 加一层前缀。
    router = APIRouter()
    router.include_router(
        static_router(Path(__file__).parent / "client"),
        prefix="/client",
    )
    routes.register(
        router,
        public=True,
        description="demo_panel 静态面板资源",
    )

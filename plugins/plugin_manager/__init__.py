"""插件管理器：外部插件的运行时装卸与启停管理界面。

声明一个设置区面板（iframe 加载本目录 client/index.html），在 web 设置
侧栏独占一格视图。路由面提供 list/load/unload/enable/disable 五个操作，
全部委托 SupervisorState 的 plugin_admin_* 方法（supervisor/state.py），
装卸事件从那里统一发射，本插件不自行发事件。

engine 进程没有 routes face，register() 直接跳过——本插件只在 supervisor
进程有后端行为。

自保护：SupervisorState 拒绝对 plugin_manager 自身的 unload/disable
（卸掉自己等于管理界面当场失联）。
"""

from pathlib import Path

PLUGIN_META = {
    "name": "plugin_manager",
    "version": "1.0.0",
    "description": "插件管理器：外部插件运行时装卸/启停 + 设置区管理界面",
    "author": "clonoth",
    "web": {
        "panels": [
            {
                "id": "manager",
                # settings 槽位：出现在 web 设置侧栏，独占主区视图
                "slot": "settings",
                "title": "插件",
                "entry": "/v1/plugins/plugin_manager/web/",
            }
        ],
    },
}


def register(ctx) -> None:
    """挂管理路由与静态界面资源。engine 进程没有 routes face，直接跳过。"""
    routes = getattr(ctx.contributions, "get", lambda _name: None)("routes")
    if routes is None:
        return

    from fastapi import APIRouter, HTTPException, Request

    from engine.faces.routes import static_router

    router = APIRouter()

    def _state(request: Request):
        st = getattr(request.app.state, "state", None)
        if st is None or not hasattr(st, "plugin_admin_list"):
            raise HTTPException(status_code=503, detail="SupervisorState unavailable")
        return st

    @router.get("/list")
    async def _list(request: Request) -> dict:
        return {"plugins": _state(request).plugin_admin_list()}

    def _run(request: Request, entry: str, op: str) -> dict:
        st = _state(request)
        try:
            fn = {
                "load": st.plugin_admin_load,
                "unload": st.plugin_admin_unload,
                "enable": st.plugin_admin_enable,
                "disable": st.plugin_admin_disable,
            }[op]
            return fn(entry)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/{entry}/load")
    async def _load(entry: str, request: Request) -> dict:
        return _run(request, entry, "load")

    @router.post("/{entry}/unload")
    async def _unload(entry: str, request: Request) -> dict:
        return _run(request, entry, "unload")

    @router.post("/{entry}/enable")
    async def _enable(entry: str, request: Request) -> dict:
        return _run(request, entry, "enable")

    @router.post("/{entry}/disable")
    async def _disable(entry: str, request: Request) -> dict:
        return _run(request, entry, "disable")

    routes.register(router, description="plugin_manager 管理操作（需鉴权）")

    client = APIRouter()
    client.include_router(
        static_router(Path(__file__).parent / "web"),
        prefix="/web",
    )
    routes.register(client, public=True, description="plugin_manager 管理界面静态资源")

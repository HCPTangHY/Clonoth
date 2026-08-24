"""Web IDE 插件：工作区文件的查看与编辑能力。

第一步交付：文件预览（内嵌于宿主文件树面板的槽位脚本）。
第二步交付：接管宿主 files overlay——面板声明 replaces:'files'，启用后
点工作区 pill 打开的是本插件的自含面板（文件树 + 预览 + 后续编辑配置），
宿主内置 WorkspaceFileTree 仅在插件未启用时作为兑底。
第三步交付：文件编辑。面板中文本文件可直接编辑，保存走本插件注册的
PUT /v1/plugins/ide/file 端点。

面板是自含 HTML 页，经 routes face 静态路由服务；数据走 supervisor REST
（workspace/tree、sessions/{id}/file），鉴权由宿主 boot 对象注入。
写端点由本插件注册（默认鉴权），写范围比读端点更严格：仅会话工作区内，
无 workspace_root/data/ 例外。
"""

from pathlib import Path

PLUGIN_META = {
    "name": "ide",
    "version": "0.5.0",
    "description": "Web IDE：接管工作区文件面板（标签栏切换、文本编辑保存）",
    "author": "clonoth",
    "client": {
        "panels": [
            {
                "id": "workspace",
                "slot": "right",
                "title": "IDE",
                # 接管宿主内置 files overlay，不作为独立入口出现在 Header。
                "replaces": "files",
                "entry": "/v1/plugins/ide/client/index.html",
            }
        ],
        "slots": [
            {
                "slot_id": "ide.file_preview",
                "slot": "workspace_file_preview",
                "priority": 50,
                # 预览区语义上独占：任一时刻只显示一个文件的内容。
                "mode": "replace",
                "script": {"file": "client/preview.js"},
            }
        ],
        "styles": {"file": "client/preview.css"},
    },
}


def register(ctx) -> None:
    """挂写文件端点与面板静态资源路由。engine 进程没有 routes face，直接跳过。"""
    routes = getattr(ctx.contributions, "get", lambda _name: None)("routes")
    if routes is None:
        return

    from fastapi import APIRouter, HTTPException, Request

    from engine.faces.routes import static_router

    # ── 写文件端点 ───────────────────────────────────────────────────
    # 写范围比读端点（supervisor/api.py session_file）更严格：仅会话工作区
    # 内（未设工作区时兑底 workspace_root），不给 data/ 例外。鉴权依赖全局
    # /v1/ 中间件（public 默认 False）。
    MAX_WRITE_BYTES = 4 * 1024 * 1024

    api = APIRouter()

    def _state(request: Request):
        st = getattr(request.app.state, "state", None)
        if st is None or not hasattr(st, "get_session_workspace"):
            raise HTTPException(status_code=503, detail="SupervisorState unavailable")
        return st

    @api.put("/file")
    async def _save_file(request: Request) -> dict:
        st = _state(request)
        session_id = str(request.query_params.get("session_id") or "").strip()
        raw_path = str(request.query_params.get("path") or "").replace("\\", "/").strip()
        if not raw_path:
            raise HTTPException(status_code=400, detail="empty path")

        ws_root = st.workspace_root.resolve()
        session_workspace: Path | None = None
        ws_info = st.get_session_workspace(session_id) if session_id else None
        if ws_info and ws_info.get("path"):
            session_workspace = Path(ws_info["path"]).resolve()

        base = session_workspace or ws_root
        if raw_path.startswith("/"):
            target = Path(raw_path).resolve()
        else:
            target = (base / raw_path).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            raise HTTPException(status_code=403, detail="path outside session workspace")

        body = await request.body()
        if len(body) > MAX_WRITE_BYTES:
            raise HTTPException(status_code=413, detail="file too large")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return {"path": raw_path, "bytes": len(body)}

    routes.register(api, description="ide 写文件端点（默认鉴权）")

    # ── 面板静态资源 ─────────────────────────────────────────────────
    client = APIRouter()
    client.include_router(
        static_router(Path(__file__).parent / "client"),
        prefix="/client",
    )
    routes.register(client, public=True, description="ide 面板静态资源")

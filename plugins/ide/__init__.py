"""Web IDE 插件：工作区文件的查看与编辑能力。

第一步交付：文件预览（内嵌于宿主文件树面板的槽位脚本）。
第二步交付：接管宿主 files overlay——面板声明 replaces:'files'，启用后
点工作区 pill 打开的是本插件的自含面板（文件树 + 预览 + 后续编辑配置），
宿主内置 WorkspaceFileTree 仅在插件未启用时作为兑底。

面板是自含 HTML 页，经 routes face 静态路由服务；数据走 supervisor REST
（workspace/tree、sessions/{id}/file），鉴权由宿主 boot 对象注入。
"""

from pathlib import Path

PLUGIN_META = {
    "name": "ide",
    "version": "0.3.0",
    "description": "Web IDE：接管工作区文件面板（左右分栏 + 顶部多标签，VSCode 预览/固定语义）",
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
    """挂面板静态资源路由。engine 进程没有 routes face，直接跳过。"""
    routes = getattr(ctx.contributions, "get", lambda _name: None)("routes")
    if routes is None:
        return

    from fastapi import APIRouter

    from engine.faces.routes import static_router

    client = APIRouter()
    client.include_router(
        static_router(Path(__file__).parent / "client"),
        prefix="/client",
    )
    routes.register(client, public=True, description="ide 面板静态资源")

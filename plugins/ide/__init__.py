"""Web IDE 插件：工作区文件的查看与编辑能力。第一步交付文件预览。

宿主在工作区文件树面板预留 workspace_file_preview 槽位（点击文件时打开），
本插件声明该槽位并负责内容渲染：文本按行号展示，图片直接显示，二进制提示。
文件内容经 ctx.api.request 走 session 文件端点（鉴权由宿主注入）。

无后端注册动作：不挂 hook、不注册路由，纯前端贡献。
"""

PLUGIN_META = {
    "name": "ide",
    "version": "0.1.0",
    "description": "Web IDE：工作区文件预览（文件树点击打开，文本/图片渲染）",
    "author": "clonoth",
    "client": {
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
    """纯前端贡献插件：无后端注册动作。"""
    return None

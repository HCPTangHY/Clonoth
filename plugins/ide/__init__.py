"""Web IDE 插件：工作区文件的查看、编辑与引用能力。

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

import re
from pathlib import Path

PLUGIN_META = {
    "name": "ide",
    "version": "0.8.0",
    "description": "Web IDE：文件面板 + 编辑器 + @文件引用（输入框补全与请求时展开）",
    "author": "clonoth",
    # supervisor：静态面板 + 写端点；engine：before_llm_call 引用展开。
    "processes": ["supervisor", "engine"],
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
            },
            {
                # 输入框 @ 补全：渲染在输入框上方浮动区，检测宿主锚定的
                # textarea（data-composer-textarea），选中后经宿主
                # insertComposerText action 写回。
                "slot_id": "ide.completer",
                "slot": "input_above",
                "priority": 40,
                "mode": "append",
                "script": {"file": "client/completer.js"},
            },
        ],
        "styles": {"file": "client/preview.css"},
    },
}


# ── @引用展开（engine 进程，before_llm_call） ────────────────────
# 语义：用户消息中的 @path 在落库（JSONL）与前端渲染中保持字面量；
# 仅在每次 LLM 出站请求前展开为文件内容附加段（附加在首次引用该文件的
# 消息尾部）。同一文件在一次请求内只展开一次。fallback 路径
# （fallback_provider 重发）不经过此 hook，引用不展开，属已知边界。

_MENTION_RE = re.compile(r"@([A-Za-z0-9_./~:-]+)")
_MAX_EXPAND_CHARS = 32 * 1024
_expand_cache: dict[str, tuple[float, str | None]] = {}


def _read_truncated(target: Path) -> str | None:
    """读文件并截断，mtime 缓存；二进制或不可解码文件返回 None。"""
    key = target.as_posix()
    try:
        mtime_ns = target.stat().st_mtime_ns
    except OSError:
        _expand_cache.pop(key, None)
        return None
    cached = _expand_cache.get(key)
    if cached and cached[0] == mtime_ns:
        return cached[1]
    try:
        raw = target.read_bytes()
    except OSError:
        return None
    text: str | None = None
    if b"\x00" not in raw[:4096]:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
    if text is not None and len(text) > _MAX_EXPAND_CHARS:
        text = text[:_MAX_EXPAND_CHARS] + "\n... (truncated)"
    _expand_cache[key] = (mtime_ns, text)
    return text


def _msg_text_and_writer(msg: dict):
    """返回 (text, writer)。str content 直接写；multimodal 写第一个 text 部件。"""
    content = msg.get("content")
    if isinstance(content, str):
        return content, lambda t: msg.__setitem__("content", t)
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                orig = str(part.get("text") or "")
                return orig, (lambda p: (lambda t: p.__setitem__("text", t)))(part)
    return None, None


def _expand_file_mentions(ctx) -> None:
    """before_llm_call handler：展开 user 消息中的工作区 @path 引用。"""
    ws_root = ctx.extra.get("workspace_root")
    if not ws_root and ctx.rctx is not None:
        ws_root = getattr(ctx.rctx, "workspace_root", None)
    if not ws_root:
        return
    ws_root = Path(ws_root).resolve()
    expanded: set[str] = set()
    for msg in ctx.messages:
        if msg.get("role") != "user":
            continue
        text, writer = _msg_text_and_writer(msg)
        if not text or "@" not in text:
            continue
        parts: list[str] = []
        for m in _MENTION_RE.finditer(text):
            raw = m.group(1).lstrip("./")
            if not raw or ("." not in raw and "/" not in raw):
                continue
            target = (ws_root / raw).resolve()
            try:
                target.relative_to(ws_root)
            except ValueError:
                continue
            if not target.is_file():
                continue
            key = target.as_posix()
            if key in expanded:
                continue
            body = _read_truncated(target)
            if body is None:
                continue
            expanded.add(key)
            parts.append(f"[Referenced file: {raw}]\n```\n{body}\n```")
        if parts:
            writer(text + "\n\n" + "\n\n".join(parts))


def register(ctx) -> None:
    """双进程入口：supervisor 挂路由；engine 挂 @引用展开 hook。"""
    routes = getattr(ctx.contributions, "get", lambda _name: None)("routes")
    if routes is None:
        # engine 进程：注册 before_llm_call 展开处理。
        dispose = ctx.hooks.register("before_llm_call", _expand_file_mentions, priority=50)
        return [dispose]

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

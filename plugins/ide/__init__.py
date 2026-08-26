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
from typing import Any

PLUGIN_META = {
    "name": "ide",
    "version": "0.11.0",
    "description": "Web IDE：文件面板 + 编辑器 + @文件引用（输入框补全与请求时展开）",
    "author": "clonoth",
    # supervisor：静态面板 + 写端点；engine：before_llm_call 引用展开。
    "processes": ["supervisor", "engine"],
    "web": {
        "panels": [
            {
                "id": "workspace",
                "slot": "right",
                "title": "IDE",
                # 接管宿主内置 files overlay，不作为独立入口出现在 Header。
                "replaces": "files",
                "entry": "/v1/plugins/ide/web/index.html",
            }
        ],
        "slots": [
            {
                "slot_id": "ide.file_preview",
                "slot": "workspace_file_preview",
                "priority": 50,
                # 预览区语义上独占：任一时刻只显示一个文件的内容。
                "mode": "replace",
                "script": {"file": "web/preview.js"},
            },
            {
                # 输入框 @ 补全：渲染在输入框上方浮动区，检测宿主锚定的
                # textarea（data-composer-textarea），选中后经宿主
                # insertComposerText action 写回。
                "slot_id": "ide.completer",
                "slot": "input_above",
                "priority": 40,
                "mode": "append",
                "script": {"file": "web/completer.js"},
            },
            {
                # 工具卡内容区接管：execute_command 分块可视化
                "slot_id": "ide.tool_execute_command",
                "slot": "tool_card_content:execute_command",
                "priority": 50,
                "mode": "replace",
                "script": {"file": "web/renderers/execute_command.js"},
            },
            {
                # apply_diff 红绿对比 + 在 IDE 打开
                "slot_id": "ide.tool_apply_diff",
                "slot": "tool_card_content:apply_diff",
                "priority": 50,
                "mode": "replace",
                "script": {"file": "web/renderers/apply_diff.js"},
            },
            {
                # read_file 文件 chip 列表 + 分节内容块
                "slot_id": "ide.tool_read_file",
                "slot": "tool_card_content:read_file",
                "priority": 50,
                "mode": "replace",
                "script": {"file": "web/renderers/read_file.js"},
            },
            {
                # write_file 路径 chip + 内容预览折叠
                "slot_id": "ide.tool_write_file",
                "slot": "tool_card_content:write_file",
                "priority": 50,
                "mode": "replace",
                "script": {"file": "web/renderers/write_file.js"},
            },
            {
                # list_dir 目录树文本块
                "slot_id": "ide.tool_list_dir",
                "slot": "tool_card_content:list_dir",
                "priority": 50,
                "mode": "replace",
                "script": {"file": "web/renderers/list_dir.js"},
            },
            {
                # grep/search_in_files 匹配按文件分组列表
                "slot_id": "ide.tool_grep",
                "slot": "tool_card_content:grep",
                "priority": 50,
                "mode": "replace",
                "script": {"file": "web/renderers/grep.js"},
            },
        ],
        # 消息注解器：识别助手消息行内代码里的工作区路径，渲染为可点链接，
        # 点击经 openPanel 打开 files 面板（ide 已接管）并转交 open-file 意图。
        "annotators": [
            {
                "id": "ide.file_link",
                "priority": 50,
                "script": {"file": "web/annotator.js"},
            },
        ],
        "styles": {"file": "web/preview.css"},
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
    # 优先 session 工作区（与前端 session 级文件树同基准），回退 engine 运行根。
    ws_root = ctx.extra.get("workspace")
    if not ws_root and ctx.rctx is not None:
        ws_root = getattr(ctx.rctx, "workspace", None)
    if not ws_root:
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
            # 只剥 ./ 前缀对，不逐字符剥点——保住 .env.example 这类点开头文件。
            raw = m.group(1)
            while raw.startswith("./"):
                raw = raw[2:]
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

    # ── git 只读端点 ─────────────────────────────────────────────────
    # 会话工作区内的 git status 与单文件 unified diff。子进程超时 10s，
    # 非 git 仓库返回 is_repo=False 而不是错误，面板据此显示提示文案。
    import asyncio

    async def _git(cwd: Path, *git_args: str) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", *git_args,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return -1, "", "git 命令超时"
            return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        except FileNotFoundError:
            return -1, "", "git 未安装"

    def _git_base(request: Request) -> Path:
        st = _state(request)
        session_id = str(request.query_params.get("session_id") or "").strip()
        ws_info = st.get_session_workspace(session_id) if session_id else None
        if ws_info and ws_info.get("path"):
            return Path(ws_info["path"]).resolve()
        return st.workspace_root.resolve()

    @api.get("/git/status")
    async def _git_status(request: Request) -> dict:
        base = _git_base(request)
        rc, branch, _ = await _git(base, "rev-parse", "--abbrev-ref", "HEAD")
        if rc != 0:
            return {"is_repo": False, "branch": "", "changes": [], "ahead": 0, "behind": 0, "unpushed": []}
        rc, out, _ = await _git(base, "status", "--porcelain=v1", "-uall")
        if rc != 0:
            return {"is_repo": False, "branch": "", "changes": [], "ahead": 0, "behind": 0, "unpushed": []}
        changes: list[dict[str, str]] = []
        for line in out.splitlines():
            if len(line) < 4:
                continue
            xy = line[:2]
            raw = line[3:]
            # 重命名格式 "R  old -> new"，取新路径
            path = raw.split(" -> ")[-1].strip().strip('"')
            status = "?" if xy == "??" else ("A" if "A" in xy else ("D" if "D" in xy else ("R" if "R" in xy else "M")))
            changes.append({"path": path, "status": status, "staged": "true" if xy[0] not in (" ", "?") else ""})

        # 与远程的差异：upstream 追踪 + ahead/behind + 未推送提交列表。
        # 无 upstream（本地分支）时三个字段为零值，面板不显示该分组。
        ahead = 0
        behind = 0
        unpushed: list[dict[str, Any]] = []
        rc, upstream, _ = await _git(base, "rev-parse", "--abbrev-ref", "@{u}")
        if rc == 0 and upstream.strip():
            rc, counts, _ = await _git(base, "rev-list", "--left-right", "--count", "@{u}...HEAD")
            if rc == 0:
                parts = counts.split()
                if len(parts) == 2:
                    try:
                        behind, ahead = int(parts[0]), int(parts[1])
                    except ValueError:
                        pass
            if ahead > 0:
                rc, log_out, _ = await _git(base, "log", "@{u}..HEAD", "-20", "--pretty=format:%h%x1f%an%x1f%at%x1f%s")
                if rc == 0:
                    for line in log_out.splitlines():
                        fields = line.split("\x1f")
                        if len(fields) != 4:
                            continue
                        unpushed.append({"hash": fields[0], "author": fields[1], "time": int(fields[2]), "subject": fields[3]})
        return {
            "is_repo": True, "branch": branch.strip(), "changes": changes,
            "ahead": ahead, "behind": behind, "unpushed": unpushed,
        }

    @api.get("/git/diff")
    async def _git_diff(request: Request) -> dict:
        base = _git_base(request)
        raw_path = str(request.query_params.get("path") or "").replace("\\", "/").strip()
        if not raw_path:
            raise HTTPException(status_code=400, detail="empty path")
        target = (base / raw_path).resolve() if not raw_path.startswith("/") else Path(raw_path).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            raise HTTPException(status_code=403, detail="path outside session workspace")
        rel = target.relative_to(base).as_posix()
        rc, out, err = await _git(base, "diff", "--", rel)
        if rc != 0:
            raise HTTPException(status_code=500, detail=err.strip() or "git diff 失败")
        if not out.strip():
            # 未跟踪文件没有 diff 输出；用 no-index 对比空文件得到全量新增视图
            rc, out, _ = await _git(base, "diff", "--no-index", "--", "/dev/null", rel)
            # --no-index 有差异时退出码为 1，不是错误
            if rc not in (0, 1):
                out = ""
        return {"path": rel, "diff": out}

    def _git_rel(base: Path, raw_path: str) -> str:
        """路径守卫 + 转为仓库相对路径，供 stage/unstage/diff 共用。"""
        raw = raw_path.replace("\\", "/").strip()
        if not raw:
            raise HTTPException(status_code=400, detail="empty path")
        target = (base / raw).resolve() if not raw.startswith("/") else Path(raw).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            raise HTTPException(status_code=403, detail="path outside session workspace")
        return target.relative_to(base).as_posix()

    @api.get("/git/log")
    async def _git_log(request: Request) -> dict:
        base = _git_base(request)
        rc, out, _ = await _git(base, "log", "-30", "--pretty=format:%h%x1f%an%x1f%at%x1f%s")
        if rc != 0:
            return {"is_repo": False, "commits": []}
        commits = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 4:
                continue
            commits.append({"hash": parts[0], "author": parts[1], "time": int(parts[2]), "subject": parts[3]})
        return {"is_repo": True, "commits": commits}

    # ── 提交详情与远程信息 ────────────────────────────────────────────

    def _sanitize_remote_url(url: str) -> str:
        """剥离 remote URL 中的凭证，只保留 host/path。"""
        # https://user:token@github.com/org/repo.git -> github.com/org/repo
        m = re.match(r"https?://(?:[^@/]+@)?([^/]+)(/.*)", url)
        if m:
            path = m.group(2)
            if path.endswith(".git"):
                path = path[:-4]
            return m.group(1) + path
        # git@github.com:org/repo.git -> github.com/org/repo
        m = re.match(r"git@([^:]+):(.+)", url)
        if m:
            path = m.group(2)
            if path.endswith(".git"):
                path = path[:-4]
            return m.group(1) + "/" + path
        return url

    @api.get("/git/remote")
    async def _git_remote(request: Request) -> dict:
        base = _git_base(request)
        rc, url, _ = await _git(base, "config", "--get", "remote.origin.url")
        remote = _sanitize_remote_url(url.strip()) if rc == 0 and url.strip() else ""
        rc, upstream, _ = await _git(base, "rev-parse", "--abbrev-ref", "@{u}")
        upstream_name = upstream.strip() if rc == 0 else ""
        fork_point: dict[str, Any] | None = None
        if upstream_name:
            rc, mb, _ = await _git(base, "merge-base", "HEAD", "@{u}")
            if rc == 0 and mb.strip():
                rc2, subj, _ = await _git(base, "log", "-1", "--pretty=format:%s", mb.strip())
                fork_point = {"hash": mb.strip()[:10], "subject": subj.strip() if rc2 == 0 else ""}
        return {"remote": remote, "upstream": upstream_name, "fork_point": fork_point}

    @api.get("/git/show")
    async def _git_show(request: Request) -> dict:
        base = _git_base(request)
        commit_hash = str(request.query_params.get("hash") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{4,40}", commit_hash):
            raise HTTPException(status_code=400, detail="invalid hash")
        # 提交元信息
        rc, meta, _ = await _git(base, "log", "-1", "--pretty=format:%H%x1f%an%x1f%ae%x1f%at%x1f%B", commit_hash)
        if rc != 0:
            raise HTTPException(status_code=404, detail="commit not found")
        parts = meta.split("\x1f", 4)
        if len(parts) < 5:
            raise HTTPException(status_code=500, detail="unexpected git log output")
        # 变更文件列表（--numstat：added deleted path）
        rc2, stat, _ = await _git(base, "show", "--numstat", "--pretty=format:", commit_hash)
        files: list[dict[str, Any]] = []
        if rc2 == 0:
            for line in stat.splitlines():
                fields = line.split("\t")
                if len(fields) != 3:
                    continue
                try:
                    added = int(fields[0]) if fields[0] != "-" else 0
                    deleted = int(fields[1]) if fields[1] != "-" else 0
                except ValueError:
                    continue
                files.append({"path": fields[2], "added": added, "deleted": deleted})
        return {
            "hash": parts[0],
            "author": parts[1],
            "email": parts[2],
            "time": int(parts[3]),
            "message": parts[4].strip(),
            "files": files,
        }

    @api.get("/git/commit_diff")
    async def _git_commit_diff(request: Request) -> dict:
        base = _git_base(request)
        commit_hash = str(request.query_params.get("hash") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{4,40}", commit_hash):
            raise HTTPException(status_code=400, detail="invalid hash")
        raw_path = str(request.query_params.get("path") or "").strip()
        if not raw_path:
            raise HTTPException(status_code=400, detail="empty path")
        rel = _git_rel(base, raw_path)
        rc, out, err = await _git(base, "diff", commit_hash + "^", commit_hash, "--", rel)
        if rc != 0:
            # 根提交没有父提交，用空树对比
            rc, out, _ = await _git(base, "diff", "4b825dc642cb6eb9a060e54bf8d69288fbee4904", commit_hash, "--", rel)
            if rc != 0:
                raise HTTPException(status_code=500, detail=err.strip() or "git diff 失败")
        return {"path": rel, "diff": out}

    @api.post("/git/stage")
    async def _git_stage(request: Request) -> dict:
        base = _git_base(request)
        rel = _git_rel(base, str(request.query_params.get("path") or ""))
        rc, _, err = await _git(base, "add", "--", rel)
        if rc != 0:
            raise HTTPException(status_code=500, detail=err.strip() or "git add 失败")
        return {"ok": True, "path": rel}

    @api.post("/git/unstage")
    async def _git_unstage(request: Request) -> dict:
        base = _git_base(request)
        rel = _git_rel(base, str(request.query_params.get("path") or ""))
        rc, _, err = await _git(base, "reset", "-q", "--", rel)
        if rc != 0:
            raise HTTPException(status_code=500, detail=err.strip() or "git reset 失败")
        return {"ok": True, "path": rel}

    @api.post("/git/commit")
    async def _git_commit(request: Request) -> dict:
        base = _git_base(request)
        body = await request.body()
        message = body.decode("utf-8", "replace").strip()
        if not message:
            raise HTTPException(status_code=400, detail="empty commit message")
        if len(message) > 4096:
            raise HTTPException(status_code=400, detail="commit message too long")
        # 提交信息经 stdin 传入，不经过命令行拼接，无注入面
        proc = await asyncio.create_subprocess_exec(
            "git", "commit", "-F", "-",
            cwd=str(base),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(message.encode("utf-8")), timeout=15.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise HTTPException(status_code=504, detail="git commit 超时")
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail=err.decode("utf-8", "replace").strip() or "git commit 失败")
        return {"ok": True, "output": out.decode("utf-8", "replace").strip()}

    # register 必须在所有路由定义之后：已 attach 状态下 register 立即
    # include_router，只拷贝当时已存在的路由；之后定义的路由不会追加。
    routes.register(api, description="ide 写文件与 git 端点（默认鉴权）")

    # ── 面板静态资源 ─────────────────────────────────────────────────
    client = APIRouter()
    client.include_router(
        static_router(Path(__file__).parent / "web"),
        prefix="/web",
    )
    routes.register(client, public=True, description="ide 面板静态资源")

"""System management: request_restart (with git snapshot helpers)."""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any

from clonoth_runtime import get_int, load_runtime_config

from ..context import ToolContext
from .._common import request_guard, safe_subprocess_env


def _ok(result_text: str, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: request_restart returns operational metadata and now
    # needs a canonical readable transcript. How: place all fields under data with
    # the result string. Purpose: keep restart output in the shared ok/data/error
    # schema while preserving structured git data.
    return {"ok": True, "data": {"result": result_text, **fields}}


def _err(message: Any, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: restart and git checkpoint failures must include
    # data.result. How: mirror optional structured fields under data and top level.
    # Purpose: keep failure details readable and backward-tolerant.
    text = str(message)
    data = {"result": f"ERROR: {text}", **fields}
    response: dict[str, Any] = {"ok": False, "error": text, "data": data}
    response.update(fields)
    return response


# ---------------------------------------------------------------------------
#  Git helpers
# ---------------------------------------------------------------------------

def _run_capture(*, args: list[str], cwd: Path, timeout_sec: float = 10.0) -> tuple[int, str]:
    try:
        cp = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=safe_subprocess_env(),
        )
        out = (cp.stdout or "") + ("\n" + cp.stderr if cp.stderr else "")
        return cp.returncode, out.strip()
    except Exception as e:
        return 999, str(e)


def _cleanup_old_artifacts(*, workspace_root: Path, current_run_id: str) -> None:
    """[2026-05-25] 用后即焚：删除非当前 run_id 的 artifact 目录。"""
    artifacts_root = workspace_root / "data" / "artifacts"
    if not artifacts_root.exists():
        return
    for d in artifacts_root.iterdir():
        if d.name == "tool_results":
            # [AutoC 2026-06-08] Why: engine tool-result artifacts are task-scoped and
            # cleaned by supervisor task completion or data_cleanup. How: skip the
            # shared tool_results root in request_restart's run_id artifact cleanup.
            # Purpose: a git snapshot from one tool cannot delete another running
            # task's recoverable large tool output.
            continue
        if d.is_dir() and d.name != current_run_id:
            try:
                import shutil
                shutil.rmtree(d)
            except Exception:
                pass


def _write_text_artifact(*, ctx: ToolContext, filename: str, text: str) -> str:
    # 写入前清理旧 artifact
    _cleanup_old_artifacts(workspace_root=ctx.workspace_root, current_run_id=ctx.run_id)
    artifacts_dir = ctx.workspace_root / "data" / "artifacts" / ctx.run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    p = artifacts_dir / filename
    p.write_text(text or "", encoding="utf-8", errors="ignore")
    return p.relative_to(ctx.workspace_root).as_posix()


def _git_snapshot(ctx: ToolContext) -> dict[str, Any]:
    root = ctx.workspace_root

    runtime_cfg = load_runtime_config(root)
    diff_max_chars = get_int(runtime_cfg, "meta.git.diff_max_chars", 400_000, min_value=10_000, max_value=2_000_000)

    rc, inside = _run_capture(args=["git", "rev-parse", "--is-inside-work-tree"], cwd=root)
    if rc != 0 or inside.strip().lower() != "true":
        return {"git_ok": False, "git_error": "not a git repo"}

    head_rc, head = _run_capture(args=["git", "rev-parse", "HEAD"], cwd=root)
    status_rc, status = _run_capture(args=["git", "status", "--porcelain"], cwd=root)
    stat_rc, diff_stat = _run_capture(args=["git", "diff", "--stat"], cwd=root, timeout_sec=20)

    diff_ref = ""
    if stat_rc == 0 and diff_stat.strip():
        diff_rc, diff_text = _run_capture(args=["git", "diff"], cwd=root, timeout_sec=30)
        if diff_rc == 0:
            if len(diff_text) > diff_max_chars:
                diff_text = diff_text[:diff_max_chars] + "\n...<truncated>"
            diff_ref = _write_text_artifact(
                ctx=ctx,
                filename=f"git_diff_{uuid.uuid4().hex}.diff",
                text=diff_text,
            )

    return {
        "git_ok": True,
        "git_head": head.strip() if head_rc == 0 else "",
        "git_dirty": bool(status.strip()) if status_rc == 0 else None,
        "git_status_porcelain": status,
        "git_diff_stat": diff_stat,
        "git_diff_ref": diff_ref,
    }


def _git_ensure_identity(*, root: Path) -> None:
    rc_name, name = _run_capture(args=["git", "config", "user.name"], cwd=root)
    rc_email, email = _run_capture(args=["git", "config", "user.email"], cwd=root)
    if rc_name == 0 and name.strip() and rc_email == 0 and email.strip():
        return
    _run_capture(args=["git", "config", "user.name", "clonoth"], cwd=root)
    _run_capture(args=["git", "config", "user.email", "clonoth@local"], cwd=root)


def _git_commit_all(*, ctx: ToolContext, message: str) -> dict[str, Any]:
    root = ctx.workspace_root

    snap_before = _git_snapshot(ctx)
    if not snap_before.get("git_ok"):
        return {"ok": False, "error": snap_before.get("git_error")}

    before_head = str(snap_before.get("git_head") or "")

    if not snap_before.get("git_dirty"):
        return {"ok": True, "committed": False, "before_head": before_head, "after_head": before_head}

    _git_ensure_identity(root=root)

    rc_add, out_add = _run_capture(args=["git", "add", "-A"], cwd=root, timeout_sec=30)
    if rc_add != 0:
        return {"ok": False, "error": "git add failed", "output": out_add, "before_head": before_head}

    rc_commit, out_commit = _run_capture(
        args=["git", "commit", "-m", message],
        cwd=root,
        timeout_sec=60,
    )

    snap_after = _git_snapshot(ctx)
    after_head = str(snap_after.get("git_head") or "") if snap_after.get("git_ok") else before_head

    if rc_commit != 0:
        return {
            "ok": False,
            "error": "git commit failed",
            "output": out_commit,
            "before_head": before_head,
            "after_head": after_head,
        }

    return {
        "ok": True,
        "committed": True,
        "output": out_commit,
        "before_head": before_head,
        "after_head": after_head,
    }


# ---------------------------------------------------------------------------
#  Tool function
# ---------------------------------------------------------------------------

async def request_restart(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = str(args.get("target", "engine"))
    reason = str(args.get("reason", ""))

    git_info = _git_snapshot(ctx)

    op, err = await request_guard(
        ctx,
        "restart",
        {
            "target": target,
            "reason": reason,
            "git": git_info,
            "will_git_commit_before_restart": True,
        },
    )
    if err is not None:
        return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    approval_id: str | None = op.get("approval_id") if op.get("safety_level") == "approval_required" else None

    commit_res = _git_commit_all(
        ctx=ctx,
        message=f"clonoth: checkpoint before restart (handoff={ctx.run_id}, target={target})",
    )

    if target in {"engine", "all"}:
        final_text = f"已请求重启：{target}。"
        if reason.strip():
            final_text += f"\n原因：{reason.strip()}"
        final_text += "\n（系统已创建 checkpoint；若新版本启动失败将自动回滚）"

        try:
            await ctx.emit_event("intermediate_reply", {"text": final_text})
        except Exception:
            pass

    # [Fork/Merge 2026-05-17] Why: restart notifications are written back to the
    # user-facing session, not the temporary branch where a tool may run. How: use
    # ToolContext's parent-first route session for the restart request. Purpose:
    # pending restart messages and outbound status reach the correct conversation.
    r = await ctx.http.post(
        f"{ctx.supervisor_url}/v1/admin/restart",
        json={"target": target, "reason": reason, "approval_id": approval_id, "session_id": ctx.route_session_id()},
    )
    if r.status_code >= 400:
        return _err(r.text, git=git_info, git_commit=commit_res)

    return _ok(
        f"Restart requested: {target}",
        target=target,
        scheduled=True,
        git=git_info,
        git_commit=commit_res,
    )

"""execute_command — shell command execution (policy + approval guarded)."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from clonoth_runtime import get_float, get_int, load_runtime_config

from ..context import ToolContext
from .._common import kill_process_group, request_guard, safe_subprocess_env


def _error_response(message: Any, **extra: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: execute_command failures must remain readable after
    # the engine starts preferring data.result. How: store the error message both in
    # error and in data.result, with optional flags such as cancelled in data and at
    # the top level when callers still inspect them. Purpose: keep cancellation and
    # timeout behavior compatible with the unified ok/data/error schema.
    text = str(message)
    payload: dict[str, Any] = {"ok": False, "error": text, "data": {"result": f"ERROR: {text}"}}
    if extra:
        payload.update(extra)
        payload["data"].update(extra)
    return payload


async def execute_command(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    command = str(args.get("command", "")).strip()

    runtime_cfg = load_runtime_config(ctx.workspace_root)
    default_timeout_sec = get_float(
        runtime_cfg,
        "meta.execute_command.default_timeout_sec",
        60.0,
        min_value=1.0,
        max_value=3600.0,
    )
    max_output_chars = get_int(
        runtime_cfg,
        "meta.execute_command.max_output_chars",
        8000,
        min_value=1000,
        max_value=200_000,
    )

    timeout_raw = args.get("timeout_sec")
    try:
        timeout_sec = float(timeout_raw) if timeout_raw is not None else float(default_timeout_sec)
    except Exception:
        timeout_sec = float(default_timeout_sec)

    _op, err = await request_guard(ctx, "execute_command", {"command": command})
    if err is not None:
        return _error_response(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    if await ctx.check_cancelled():
        return _error_response("task cancelled", cancelled=True)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(ctx.workspace_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_subprocess_env(),
            # Fix: run in new session so os.killpg can kill the entire process
            # tree, preventing orphaned grandchildren from holding pipe fds and
            # causing communicate() to hang forever.
            start_new_session=True,
        )
        waiter = asyncio.create_task(proc.communicate())
        started = time.monotonic()
        while True:
            done, _pending = await asyncio.wait({waiter}, timeout=0.2)
            if waiter in done:
                stdout_bytes, stderr_bytes = waiter.result()
                break
            if await ctx.check_cancelled():
                # Fix: kill entire process group, not just the shell
                kill_process_group(proc)
                # Fix: 5s safety timeout on gather — if killpg didn't clean
                # all grandchildren, don't hang forever waiting for pipe EOF
                try:
                    await asyncio.wait_for(asyncio.gather(waiter, return_exceptions=True), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                return _error_response("task cancelled", cancelled=True)
            if time.monotonic() - started >= timeout_sec:
                kill_process_group(proc)
                try:
                    await asyncio.wait_for(asyncio.gather(waiter, return_exceptions=True), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                return _error_response(f"timeout after {timeout_sec}s")

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
        out = (stdout_text or "") + ("\n" + stderr_text if stderr_text else "")
        if len(out) > max_output_chars:
            out = out[:max_output_chars] + "\n...<truncated>"
        rc = int(proc.returncode or 0)
        # [AutoC 2026-05-31] Why: shell return codes are command results, not tool
        # contract failures, so non-zero rc must still return ok=true. How: put the
        # readable transcript in data.result and keep returncode/output as nested
        # structured fields. Purpose: conform to ok/data/error without changing
        # execute_command's shell semantics.
        return {"ok": True, "data": {"result": f"returncode={rc}\n{out}", "returncode": rc, "output": out}}
    except Exception as e:
        return _error_response(e)

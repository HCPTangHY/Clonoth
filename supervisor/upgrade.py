from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clonoth_runtime import get_float, get_int, load_runtime_config

from .eventlog import SYSTEM_SESSION_ID
from .process_manager import ProcessManager
from .state import SupervisorState


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _marker_path(workspace_root: Path) -> Path:
    return workspace_root / "data" / "upgrade_pending.json"


def load_marker(*, workspace_root: Path) -> dict[str, Any] | None:
    p = _marker_path(workspace_root)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def save_marker(*, workspace_root: Path, marker: dict[str, Any]) -> None:
    p = _marker_path(workspace_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_marker(*, workspace_root: Path) -> None:
    p = _marker_path(workspace_root)
    try:
        if p.exists():
            p.unlink()
    except Exception:
        pass


def _run_git(workspace_root: Path, args: list[str], timeout_sec: float = 10.0) -> tuple[int, str]:
    try:
        cp = subprocess.run(
            ["git", *args],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        out = (cp.stdout or "") + ("\n" + cp.stderr if cp.stderr else "")
        return cp.returncode, out.strip()
    except Exception as e:
        return 999, str(e)


def git_head(workspace_root: Path) -> str | None:
    rc, out = _run_git(workspace_root, ["rev-parse", "HEAD"], timeout_sec=10)
    return out.strip() if rc == 0 and out.strip() else None


def git_reset_hard(workspace_root: Path, commit: str) -> tuple[bool, str]:
    if not commit:
        return False, "empty rollback commit"

    rc, out = _run_git(workspace_root, ["reset", "--hard", commit], timeout_sec=30)
    if rc != 0:
        return False, out
    return True, out


def compute_rollback_head_from_approval(state: SupervisorState, approval_id: str | None) -> str | None:
    if not approval_id:
        return None
    a = state.approvals.get(approval_id)
    if a is None:
        return None

    details = a.details or {}
    if not isinstance(details, dict):
        return None

    git_info = details.get("git")
    if isinstance(git_info, dict):
        head = git_info.get("git_head")
        if isinstance(head, str) and head.strip():
            return head.strip()
    return None


def _healthcheck_kernel(*, state: SupervisorState, since: datetime, prev_worker_id: str | None, pm: ProcessManager | None) -> tuple[bool, str]:
    if pm is not None:
        if pm.kernel is None or pm.kernel.popen.poll() is not None:
            return False, "kernel process not running"

    last_seen, last_wid = state.kernel_seen_snapshot()
    if last_seen is None:
        return False, "kernel not seen yet"
    if last_seen <= since:
        return False, "kernel heartbeat not updated after restart"
    if prev_worker_id is not None and last_wid == prev_worker_id:
        return False, "kernel worker_id unchanged"

    return True, "ok"


def _healthcheck_shell(*, since_pid: int | None, pm: ProcessManager | None) -> tuple[bool, str]:
    if pm is None:
        return False, "process manager not enabled"
    if pm.shell is None or pm.shell.popen.poll() is not None:
        return False, "shell process not running"
    if since_pid is not None and pm.shell.popen.pid == since_pid:
        return False, "shell pid unchanged"
    return True, "ok"


class UpgradeWatchdog:
    """Background watchdog for self-evolution upgrades.

    How it works:
    - `/v1/admin/restart` (when called with approval_id) writes `data/upgrade_pending.json`.
    - This watchdog verifies the restarted target becomes healthy within timeout.
    - On failure, it auto-rolls back via `git reset --hard <rollback_head>` and restarts again.

    Limitations:
    - If the supervisor cannot start at all (syntax error before main), we cannot self-rollback.
    - This is a best-effort safety net (no OS sandbox).
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        state: SupervisorState,
        process_manager: ProcessManager | None,
        poll_interval_sec: float = 0.5,
    ) -> None:
        self.workspace_root = workspace_root
        self.state = state
        self.pm = process_manager
        self.poll_interval_sec = poll_interval_sec

        self._started = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                self._check_once()
            except Exception:
                pass
            time.sleep(self.poll_interval_sec)

    def _check_once(self) -> None:
        marker = load_marker(workspace_root=self.workspace_root)
        if marker is None:
            return

        # schema validation (light)
        target = str(marker.get("target") or "")
        if target not in {"shell", "kernel", "all"}:
            clear_marker(workspace_root=self.workspace_root)
            return

        phase = str(marker.get("phase") or "candidate")
        if phase not in {"candidate", "rollback"}:
            phase = "candidate"

        try:
            since = datetime.fromisoformat(str(marker.get("phase_started_at")))
        except Exception:
            since = _now()

        timeout_sec = marker.get("timeout_sec")
        try:
            timeout_val = float(timeout_sec) if timeout_sec is not None else 15.0
        except Exception:
            timeout_val = 15.0

        prev_kernel_worker_id = marker.get("prev_kernel_worker_id")
        if not isinstance(prev_kernel_worker_id, str):
            prev_kernel_worker_id = None

        prev_shell_pid = marker.get("prev_shell_pid")
        if not isinstance(prev_shell_pid, int):
            prev_shell_pid = None

        # healthcheck
        ok = True
        reasons: list[str] = []

        if target in {"kernel", "all"}:
            k_ok, k_reason = _healthcheck_kernel(
                state=self.state,
                since=since,
                prev_worker_id=prev_kernel_worker_id,
                pm=self.pm,
            )
            ok = ok and k_ok
            if not k_ok:
                reasons.append(f"kernel: {k_reason}")

        if target in {"shell", "all"}:
            s_ok, s_reason = _healthcheck_shell(since_pid=prev_shell_pid, pm=self.pm)
            ok = ok and s_ok
            if not s_ok:
                reasons.append(f"shell: {s_reason}")

        if ok:
            self.state.eventlog.append(
                session_id=SYSTEM_SESSION_ID,
                component="supervisor",
                type_="upgrade_verified" if phase == "candidate" else "rollback_verified",
                payload={"target": target, "marker": marker, "ts": _now().isoformat()},
            )
            clear_marker(workspace_root=self.workspace_root)
            return

        elapsed = (_now() - since).total_seconds()
        if elapsed < timeout_val:
            # still waiting
            return

        # timed out
        if phase == "rollback":
            self.state.eventlog.append(
                session_id=SYSTEM_SESSION_ID,
                component="supervisor",
                type_="rollback_failed",
                payload={"target": target, "marker": marker, "reasons": reasons, "ts": _now().isoformat()},
            )
            clear_marker(workspace_root=self.workspace_root)
            return

        # candidate failed -> rollback if possible
        attempt = marker.get("attempt")
        try:
            attempt_val = int(attempt) if attempt is not None else 0
        except Exception:
            attempt_val = 0

        max_attempts = marker.get("max_attempts")
        try:
            max_attempts_val = int(max_attempts) if max_attempts is not None else 2
        except Exception:
            max_attempts_val = 2

        if attempt_val + 1 > max_attempts_val:
            self.state.eventlog.append(
                session_id=SYSTEM_SESSION_ID,
                component="supervisor",
                type_="upgrade_failed",
                payload={"target": target, "marker": marker, "reasons": reasons, "ts": _now().isoformat()},
            )
            clear_marker(workspace_root=self.workspace_root)
            return

        rollback_head = str(marker.get("rollback_head") or "").strip()
        ok_reset, out_reset = git_reset_hard(self.workspace_root, rollback_head)
        if not ok_reset:
            self.state.eventlog.append(
                session_id=SYSTEM_SESSION_ID,
                component="supervisor",
                type_="upgrade_failed",
                payload={
                    "target": target,
                    "marker": marker,
                    "reasons": reasons,
                    "git_reset_error": out_reset,
                    "ts": _now().isoformat(),
                },
            )
            clear_marker(workspace_root=self.workspace_root)
            return

        # update marker -> rollback phase
        new_marker = dict(marker)
        new_marker["attempt"] = attempt_val + 1
        new_marker["phase"] = "rollback"
        new_marker["phase_started_at"] = _now().isoformat()
        new_marker["last_error"] = "; ".join(reasons)
        save_marker(workspace_root=self.workspace_root, marker=new_marker)

        self.state.eventlog.append(
            session_id=SYSTEM_SESSION_ID,
            component="supervisor",
            type_="upgrade_rollback",
            payload={
                "target": target,
                "marker": new_marker,
                "git_reset_output": out_reset,
                "ts": _now().isoformat(),
            },
        )

        # restart target to load rolled-back code
        if target == "kernel":
            if self.pm is not None:
                self.pm.restart_kernel()
        elif target == "shell":
            if self.pm is not None:
                self.pm.restart_shell()
        else:
            # all: stop children then execv self
            try:
                if self.pm is not None:
                    self.pm.stop_all()
            finally:
                time.sleep(0.2)
                os.execv(sys.executable, [sys.executable, *sys.argv])


def create_upgrade_marker(
    *,
    workspace_root: Path,
    state: SupervisorState,
    process_manager: ProcessManager | None,
    target: str,
    reason: str | None,
    approval_id: str | None,
) -> dict[str, Any] | None:
    """Create an upgrade marker file to enable auto-rollback.

    Only create marker when approval_id is provided (i.e. self-evolution restart).
    """

    if not approval_id:
        return None

    candidate = git_head(workspace_root)
    if not candidate:
        return None

    rollback = compute_rollback_head_from_approval(state, approval_id)
    if not rollback:
        # fallback: use parent commit if possible
        rc, out = _run_git(workspace_root, ["rev-parse", "HEAD^"], timeout_sec=10)
        rollback = out.strip() if rc == 0 and out.strip() else candidate

    # prev state to avoid false-positive healthcheck
    last_seen, last_wid = state.kernel_seen_snapshot()
    prev_kernel_worker_id = last_wid

    prev_shell_pid: int | None = None
    if process_manager is not None and process_manager.shell is not None:
        try:
            prev_shell_pid = int(process_manager.shell.popen.pid)
        except Exception:
            prev_shell_pid = None

    runtime_cfg = load_runtime_config(workspace_root)
    timeout_sec = get_float(
        runtime_cfg,
        "supervisor.upgrade_watchdog.timeout_sec",
        15.0,
        min_value=1.0,
        max_value=300.0,
    )
    max_attempts = get_int(runtime_cfg, "supervisor.upgrade_watchdog.max_attempts", 2, min_value=1, max_value=10)

    marker: dict[str, Any] = {
        "schema_version": 1,
        "id": str(uuid.uuid4()),
        "target": target,
        "reason": reason or "",
        "approval_id": approval_id,
        "candidate_head": candidate,
        "rollback_head": rollback,
        "attempt": 0,
        "max_attempts": max_attempts,
        "phase": "candidate",
        "phase_started_at": _now().isoformat(),
        "timeout_sec": timeout_sec,
        "prev_kernel_worker_id": prev_kernel_worker_id,
        "prev_shell_pid": prev_shell_pid,
    }

    save_marker(workspace_root=workspace_root, marker=marker)
    state.eventlog.append(
        session_id=SYSTEM_SESSION_ID,
        component="supervisor",
        type_="upgrade_pending",
        payload={"target": target, "marker": marker, "ts": _now().isoformat()},
    )

    return marker

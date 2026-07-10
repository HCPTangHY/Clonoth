from __future__ import annotations

import argparse
import logging
import socket
import shutil
import subprocess
import atexit
import io
import signal
import sys
import threading
import time
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from .api import create_app
from .config_store import ConfigStore
from .eventlog import EventLog
from .policy import PolicyEngine
from .process_manager import ProcessManager
from .scheduler import SchedulerThread
from .state import SupervisorState
from .types import TaskStatus


def _ensure_web_frontend(workspace_root: Path, log_func) -> None:
    """Build platform/web/frontend/dist when dependencies are already installed."""
    frontend_dir = workspace_root / "platform" / "web" / "frontend"
    dist_index = frontend_dir / "dist" / "index.html"
    package_json = frontend_dir / "package.json"
    node_modules = frontend_dir / "node_modules"
    if dist_index.is_file() or not package_json.is_file():
        return

    npm = shutil.which("npm")
    if not npm:
        log_func("[web] 前端 dist 不存在，且未找到 npm。请执行：cd platform/web/frontend && npm ci && npm run build")
        return
    if not node_modules.is_dir():
        log_func("[web] 前端 dist 不存在。请执行：cd platform/web/frontend && npm ci && npm run build")
        return

    log_func("[web] 前端 dist 不存在，开始自动执行 npm run build")
    try:
        result = subprocess.run(
            [npm, "run", "build"],
            cwd=str(frontend_dir),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
            check=False,
        )
    except Exception as exc:
        log_func(f"[web] 前端自动构建失败：{exc}")
        log_func("[web] 请手动执行：cd platform/web/frontend && npm ci && npm run build")
        return

    if result.returncode != 0:
        output = (result.stdout or "").strip()
        tail = output[-4000:] if output else ""
        log_func(f"[web] 前端自动构建失败，退出码 {result.returncode}")
        if tail:
            log_func(tail)
        log_func("[web] 请手动执行：cd platform/web/frontend && npm ci && npm run build")
        return

    log_func("[web] 前端自动构建完成")


def main() -> None:
    # [2026-05-14] override=True: .env 文件值覆盖继承的环境变量。
    # 多实例部署时，每个实例读自己 cwd 下的 .env，互不干扰。
    load_dotenv(override=True)

    parser = argparse.ArgumentParser(description="Clonoth Supervisor")
    parser.add_argument("--host", default=os.getenv("CLONOTH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CLONOTH_PORT", "8765")))
    parser.add_argument("--no-shell", action="store_true", help="deprecated no-op; shell UI has been removed")
    parser.add_argument("--no-kernel", action="store_true", help="do not spawn kernel runtime")
    parser.add_argument("--no-workers", action="store_true", help="do not spawn any workers")
    parser.add_argument("--log-level", default=os.getenv("CLONOTH_LOG_LEVEL", "info"))
    parser.add_argument(
        "--access-log",
        action="store_true",
        help="enable uvicorn access log (VERY noisy because workers poll endpoints frequently)",
    )
    args = parser.parse_args()

    workspace_root = Path(__file__).resolve().parents[1]
    data_dir = workspace_root / "data"
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # ---- 把 supervisor 自身的所有输出重定向到日志文件 ----
    _supervisor_log_path = log_dir / "supervisor.log"
    _supervisor_log_f = open(_supervisor_log_path, "a", encoding="utf-8", buffering=1)

    def _log(msg: str) -> None:
        """写 supervisor 日志（不写终端）。"""
        try:
            _supervisor_log_f.write(msg + "\n")
            _supervisor_log_f.flush()
        except Exception:
            pass

    # 在做任何状态变更之前，先检查端口是否可用
    # [2026-06-06] Why: during restart, the old process may not have released the
    # port yet when the new process starts (1s sleep in main.py is sometimes not
    # enough). How: retry port binding up to 10 times with 1s intervals before
    # giving up. If still occupied after retries, attempt to kill the stale PID.
    # Purpose: prevent the restart loop from silently exiting and breaking the
    # restart chain.
    _port_ready = False
    _resolved_port = args.port
    if args.port == 0:
        # --port 0: let OS pick a free port, no retry needed
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
                _s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                _s.bind((args.host, 0))
                _resolved_port = _s.getsockname()[1]
            _port_ready = True
            _log(f"[supervisor] OS 分配端口: {_resolved_port}")
        except OSError as e:
            _log(f"[supervisor] 无法绑定自动端口: {e}")
    else:
        for _attempt in range(10):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
                    _s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    _s.bind((args.host, args.port))
                _port_ready = True
                break
            except OSError:
                if _attempt == 0:
                    _log(f"[supervisor] 端口 {args.host}:{args.port} 被占用，等待释放...")
                time.sleep(1)

        if not _port_ready:
            _log(f"[supervisor] 端口 {args.host}:{args.port} 等待 10s 仍未释放，尝试强制清理")
            try:
                import subprocess as _sp
                _fuser = _sp.run(
                    ["fuser", "-k", f"{args.port}/tcp"],
                    capture_output=True, timeout=5,
                )
                _log(f"[supervisor] fuser -k 结果: rc={_fuser.returncode} stdout={_fuser.stdout.decode().strip()}")
                time.sleep(1)
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
                    _s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    _s.bind((args.host, args.port))
                _port_ready = True
            except Exception as e2:
                _log(f"[supervisor] 强制清理后仍无法绑定端口: {e2}")

    if not _port_ready:
        _log(f"[supervisor] 端口 {args.host}:{args.port} 无法使用，退出")
        return

    # Replace args.port with the resolved port for all downstream usage
    args.port = _resolved_port

    events_path = data_dir / "events.jsonl"
    config_path = data_dir / "config.yaml"

    run_id = os.urandom(16).hex()

    eventlog = EventLog(events_path, run_id=run_id)
    policy = PolicyEngine(workspace_root=workspace_root)
    state = SupervisorState(workspace_root=workspace_root, eventlog=eventlog, policy=policy)

    config_store = ConfigStore(path=config_path)
    state.write_boot_event()

    # [AutoC 2026-05-30] cancel_orphaned_tasks 已移除。
    # Why: 移除 EventLog 启动回放后，self.tasks 启动时为空，旧 task 不再被恢复。
    # How: 不再调用 cancel_orphaned_tasks，启动期清理改由 SupervisorState
    # 初始化阶段的 _reconcile_after_restart() 负责。
    # Purpose: 避免保留一个依赖旧回放路径且实际没有效果的启动步骤。

    # Check for pending restart completion → inject inbound_message (v3: self-awareness)
    _restart_pending_path = data_dir / "restart_pending.json"
    if _restart_pending_path.exists():
        try:
            import json as _rjson
            _pending = _rjson.loads(_restart_pending_path.read_text(encoding="utf-8"))
            _conv_key = _pending.get("conversation_key", "")
            _channel = _pending.get("channel", "")
            if _conv_key and _channel:
                _sid = state.get_or_create_session(channel=_channel, conversation_key=_conv_key)
                _evt = state.eventlog.append(
                    session_id=_sid,
                    component="supervisor",
                    type_="inbound_message",
                    payload={
                        "channel": _channel,
                        "conversation_key": _conv_key,
                        "text": "[系统通知] 全量重启已完成，新代码已生效。",
                    },
                )
                state.record_inbound_message_event(_evt)
                _log(f"[supervisor] Injected restart completion inbound for session {_sid}")
            else:
                _log(f"[supervisor] restart_pending.json missing conversation_key/channel, skipped")
        except Exception as e:
            _log(f"[supervisor] Failed to process restart_pending.json: {e}")
        finally:
            try:
                _restart_pending_path.unlink()
            except Exception:
                pass

    # [2026-06-06] Why: TUI and VSC plugin cannot hardcode a port number.
    # How: after binding, write the actual port to data/supervisor.port so
    # clients can discover the supervisor automatically. When --port 0 is
    # used, the OS assigns a free port. Purpose: zero-config port discovery.
    _port_file = data_dir / "supervisor.port"

    def _write_port_file(port: int) -> None:
        try:
            _port_file.write_text(str(port), encoding="utf-8")
            _log(f"[supervisor] wrote port file: {_port_file} = {port}")
        except Exception as e:
            _log(f"[supervisor] failed to write port file: {e}")

    def _remove_port_file() -> None:
        try:
            _port_file.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(_remove_port_file)
    _write_port_file(args.port)

    # Store cleanup callback for api.py's _deferred_exit to call before os._exit
    # (os._exit bypasses atexit handlers)
    os.environ["_CLONOTH_PORT_FILE"] = str(_port_file)

    base_url = f"http://{args.host}:{args.port}"

    process_manager: ProcessManager | None = None
    if not args.no_workers:
        process_manager = ProcessManager(
            supervisor_url=base_url,
            workspace_root=workspace_root,
            log_dir=data_dir / "logs",
            log_func=_log,
        )
        if not args.no_kernel:
            process_manager.start_engine()

    scheduler = SchedulerThread(state=state, workspace_root=workspace_root)
    scheduler.start()

    # ---- 后台僵尸 task 回收线程 ----
    def _reap_zombie_tasks() -> None:
        """定期回收 lease 过期超过 grace period 的僵尸 task。

        跳过 session 中存在 pending approval 的 task，因为
        等审批期间 agent 是合法阻塞状态，不应被当作僵尸回收。
        """
        from datetime import datetime, timedelta, timezone
        from .types import ApprovalStatus
        _REAP_INTERVAL = 60.0
        _GRACE = timedelta(seconds=60)
        while True:
            time.sleep(_REAP_INTERVAL)
            try:
                now = datetime.now(timezone.utc)
                with state._lock:
                    # 预先收集有 pending approval 的 session 集合
                    _sessions_with_pending_approval: set[str] = set()
                    for a in state.approvals.values():
                        if a.status == ApprovalStatus.pending:
                            _sessions_with_pending_approval.add(a.session_id)

                    _MAX_TASK_AGE = timedelta(minutes=30)  # [AutoC 2026-06-24] absolute cap
                    for task in state.tasks.values():
                        if task.status != TaskStatus.running:
                            continue
                        should_reap = False
                        reason = ""
                        if not task.lease_expires_at:
                            # [AutoC 2026-05-31] 兜底：running 且无 lease 超过 5 分钟视为僵尸
                            if task.updated_at + timedelta(minutes=5) < now:
                                should_reap = True
                                reason = "no lease, stale >5min"
                        elif task.lease_expires_at + _GRACE < now:
                            should_reap = True
                            reason = "lease expired"
                        # [AutoC 2026-06-24 / 2026-07-10 fix] absolute cap: 仅在 worker
                        # heartbeat 已停止时才基于 created_at 硬上限回收。
                        # Why: worker 活着会每 60s 续 lease 并刷新 updated_at，说明 task
                        # 仍在正常工作。之前无条件 30 分钟回收会误杀合法长任务。
                        # How: 同时检查 created_at 超龄 AND updated_at 超过 5 分钟未刷新
                        # （即 heartbeat 已停止）。Purpose: 只回收真正的幽灵 task。
                        if (not should_reap
                                and task.created_at + _MAX_TASK_AGE < now
                                and task.updated_at + timedelta(minutes=5) < now):
                            should_reap = True
                            reason = f"exceeded max age {_MAX_TASK_AGE} (heartbeat stale)"
                        if not should_reap:
                            continue
                        # [Fork/Merge 2026-05-17] skip tasks with pending approval
                        route_session_id = state._route_session_id_for_task_locked(task)
                        if task.session_id in _sessions_with_pending_approval or route_session_id in _sessions_with_pending_approval:
                            continue
                        task.cancel_requested = True
                        task.status = TaskStatus.failed
                        task.updated_at = now
                        task.lease_expires_at = None
                        task.result = {"action": "fail", "error": f"zombie reaped by background ({reason})"}
                        state._event_task_snapshot("task_completed", task)
                        # [Fork/Merge 2026-05-17] background zombie reaping
                        # follows the same terminal path as API reaping.
                        state._route_completed_task_locked(task)
                        _log(f"[zombie-reaper] reaped task {task.task_id[:12]} node={task.node_id} reason={reason}")
                    # [AutoC 2026-05-30] Why: branch 和 fresh/fork child session
                    # 历史上只标记 reset，不物理删除，sessions.json 会持续膨胀。
                    # How: 复用后台 zombie reaper 的定时锁内循环，每分钟执行一次
                    # stale registry 清理。Purpose: 旧遗留 reset 行和 24h 无活动的
                    # fresh/fork child 会话会被自动移除，accumulate 会话保留。
                    state._cleanup_stale_sessions_locked()
            except Exception as e:
                _log(f"[zombie-reaper] error: {e}")

    threading.Thread(target=_reap_zombie_tasks, daemon=True, name="zombie-reaper").start()

    _ensure_web_frontend(workspace_root, _log)
    app = create_app(state=state, process_manager=process_manager, config_store=config_store)

    env_access_log = (os.getenv("CLONOTH_ACCESS_LOG") or "").strip().lower() in {"1", "true", "yes", "y"}
    access_log = bool(args.access_log or env_access_log)

    # uvicorn 日志全部写到文件，不输出到终端。
    _uvi_log_cfg = {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {
            "file": {
                "class": "logging.FileHandler",
                "filename": str(_supervisor_log_path),
                "mode": "a",
                "encoding": "utf-8",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["file"], "level": "INFO", "propagate": False},
        },
    }

    _log(f"[supervisor] starting uvicorn on {args.host}:{args.port}")
    # 用 Server API 替代 uvicorn.run()，禁用 uvicorn 的信号捕获。
    # uvicorn.run() 会安装自己的 SIGTERM/SIGINT handler 并在收到信号时
    # 调用 sys.exit(0)，导致 restart engine 时 supervisor 被意外杀死。
    import asyncio
    _uvi_config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=access_log,
        log_config=_uvi_log_cfg,
    )
    _uvi_server = uvicorn.Server(_uvi_config)
    _uvi_server.install_signal_handlers = lambda: None  # 禁用 uvicorn 信号捕获
    # [2026-06-06] Why: _deferred_exit in api.py needs to gracefully shut down
    # the uvicorn server before os._exit to release the port. How: store the
    # server reference on app.state. Purpose: restart no longer leaves the port
    # in TIME_WAIT.
    app.state._uvicorn_server = _uvi_server
    # 用 loop.run_until_complete 替代 asyncio.run()，后者会重装 SIGINT handler
    # 信号处理交给 process_manager._install_signal_handlers + _restarting_engine flag
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_uvi_server.serve())
    finally:
        _loop.close()

    # [2026-06-07] Why: _deferred_exit now signals uvicorn to exit gracefully
    # via should_exit instead of calling os._exit(75). How: after uvicorn
    # returns, check _restart_pending and call sys.exit(75) which runs atexit
    # handlers (port file cleanup) and closes the server socket properly.
    # Purpose: the restarted process can bind the port immediately without
    # TIME_WAIT or stale PID issues.
    _restart = process_manager._restart_pending if process_manager else False
    _log(f"[supervisor] uvicorn exited, restart_pending={_restart}")
    if _restart:
        sys.exit(75)
    _log("[supervisor] normal exit")


if __name__ == "__main__":
    main()

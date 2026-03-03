from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from clonoth_runtime import get_float, load_runtime_config

from .api import create_app
from .config_store import ConfigStore
from .eventlog import EventLog
from .policy import PolicyEngine
from .process_manager import ProcessManager
from .state import SupervisorState
from .upgrade import UpgradeWatchdog


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Clonoth Supervisor")
    parser.add_argument("--host", default=os.getenv("CLONOTH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CLONOTH_PORT", "8765")))
    parser.add_argument("--no-shell", action="store_true", help="do not spawn shell worker")
    parser.add_argument("--no-kernel", action="store_true", help="do not spawn kernel worker")
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
    events_path = data_dir / "events.jsonl"
    config_path = data_dir / "config.yaml"

    # run_id 每次启动不同（用于 Boot Event Injection）
    run_id = os.urandom(16).hex()

    eventlog = EventLog(events_path, run_id=run_id)
    policy = PolicyEngine(workspace_root=workspace_root)
    state = SupervisorState(eventlog=eventlog, policy=policy)

    runtime_cfg = load_runtime_config(workspace_root)
    watchdog_poll_interval_sec = get_float(
        runtime_cfg,
        "supervisor.upgrade_watchdog.poll_interval_sec",
        0.5,
        min_value=0.1,
        max_value=5.0,
    )

    # YAML 配置（provider/base_url/key/model 等）
    config_store = ConfigStore(path=config_path)

    # 写 boot 事件
    state.write_boot_event()

    base_url = f"http://{args.host}:{args.port}"

    process_manager: ProcessManager | None = None
    if not args.no_workers:
        process_manager = ProcessManager(
            supervisor_url=base_url,
            workspace_root=workspace_root,
            log_dir=data_dir / "logs",
        )

        if not args.no_kernel:
            process_manager.start_kernel()
        if not args.no_shell:
            process_manager.start_shell()

    # Start upgrade watchdog (auto-rollback) after workers are spawned.
    # It watches `data/upgrade_pending.json` written by `/v1/admin/restart`.
    watchdog = UpgradeWatchdog(
        workspace_root=workspace_root,
        state=state,
        process_manager=process_manager,
        poll_interval_sec=watchdog_poll_interval_sec,
    )
    watchdog.start()

    app = create_app(state=state, process_manager=process_manager, config_store=config_store)

    # Access log is extremely noisy because Shell/Kernel poll endpoints frequently.
    # Disabled by default. Enable via `--access-log` or `CLONOTH_ACCESS_LOG=1` when debugging.
    env_access_log = (os.getenv("CLONOTH_ACCESS_LOG") or "").strip().lower() in {"1", "true", "yes", "y"}
    access_log = bool(args.access_log or env_access_log)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=access_log,
    )


if __name__ == "__main__":
    main()

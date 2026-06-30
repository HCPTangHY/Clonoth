"""python -m engine 启动统一 worker。"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import uuid
from pathlib import Path

# [2026-05-25] Ensure all loggers (including plugins like fallback_provider)
# output to stderr so PM2 can capture them.
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Clonoth Engine Worker")
    parser.add_argument(
        "--supervisor",
        default=os.getenv("CLONOTH_SUPERVISOR_URL", "http://127.0.0.1:8765"),
    )
    parser.add_argument(
        "--worker-id",
        default=os.getenv("CLONOTH_WORKER_ID") or str(uuid.uuid4()),
    )
    args = parser.parse_args()

    workspace_root = Path(__file__).resolve().parents[1]

    from .runner import worker_loop

    asyncio.run(
        worker_loop(
            supervisor_url=args.supervisor.rstrip("/"),
            workspace_root=workspace_root,
            worker_id=args.worker_id,
        )
    )


if __name__ == "__main__":
    main()

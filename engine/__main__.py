"""python -m engine 启动统一 worker。"""
from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from pathlib import Path


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

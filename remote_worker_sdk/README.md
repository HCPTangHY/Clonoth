# Clonoth Remote Worker SDK

This package contains a small Python SDK for connecting a remote worker to the Clonoth Supervisor broker.

## Install

```bash
cd remote_worker_sdk
python3.11 -m pip install -e .
```

## Basic usage

```python
import asyncio
import os
from clonoth_remote_worker import RemoteWorker

worker = RemoteWorker(
    worker_id="win-pc-01",
    namespace="computer",
    token=os.environ["CLONOTH_WORKER_TOKEN"],
    worker_name="Windows PC",
    max_concurrency=1,
)

@worker.tool("ping", "Return a health message.")
async def ping(args):
    return {"ok": True, "data": {"result": "pong"}}

asyncio.run(worker.run(os.environ.get("CLONOTH_SUPERVISOR_URL", "http://127.0.0.1:8765")))
```

## Authentication

The worker token is not the Admin Token. Configure it on the Supervisor side in `data/remote_workers.yaml`, using a SHA256 hash of the token.

## Protocol behavior

The SDK sends a `hello` message after connecting, keeps a heartbeat loop running, dispatches `tool_call` messages to functions registered with `@worker.tool(...)`, and cancels in-flight asyncio tasks when Supervisor sends `cancel_tool_call`.

## Computer-use example

See `examples/computer_use.py`. It requires optional local packages:

```bash
python3.11 -m pip install pyautogui mss pillow
```

Run it with:

```bash
CLONOTH_WORKER_TOKEN=... CLONOTH_SUPERVISOR_URL=http://127.0.0.1:8765 python3.11 examples/computer_use.py
```

"""Clonoth launcher.

默认启动 Supervisor，并自动拉起统一引擎进程与本地 CLI。

重启机制：
    supervisor 以退出码 75 退出表示"请重启"，
    本脚本检测到后自动重新启动。

用法：
    python main.py

也可以单独启动：
    python -m supervisor.main
    python -m engine
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

RESTART_EXIT_CODE = 75


def _read_port_file() -> int:
    """Read the port from data/supervisor.port if it exists."""
    try:
        port_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "supervisor.port")
        with open(port_file) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def _ensure_port_free(port: int, max_wait: int = 5) -> None:
    """Wait for the port to be free, force-kill occupants if needed.

    [2026-06-07] Why: os._exit(75) in old code skips socket cleanup, leaving
    the port in TIME_WAIT or held by a zombie. The new supervisor code uses
    sys.exit(75), but the FIRST restart after deploy still runs old code.
    How: the launcher (which is never restarted) checks the port between
    child process exit and the next spawn. Purpose: eliminate the restart
    loop caused by port binding failure.
    """
    if port <= 0:
        return
    import socket
    for attempt in range(max_wait):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                # Port is free
                return
        except OSError:
            if attempt == max_wait - 2:
                # Second-to-last attempt: try fuser -k
                print(f"[launcher] port {port} still occupied, running fuser -k", flush=True)
                try:
                    subprocess.call(["fuser", "-k", f"{port}/tcp"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
            time.sleep(1)
    print(f"[launcher] warning: port {port} may still be occupied after {max_wait}s", flush=True)


def main() -> None:
    while True:
        result = subprocess.call([sys.executable, "-B", "-m", "supervisor.main", *sys.argv[1:]])
        if result == RESTART_EXIT_CODE:
            port = _read_port_file()
            print(f"[launcher] supervisor exited with code {RESTART_EXIT_CODE}, ensuring port {port} is free...", flush=True)
            _ensure_port_free(port)
            print(f"[launcher] restarting supervisor...", flush=True)
            continue
        sys.exit(result)


if __name__ == "__main__":
    main()

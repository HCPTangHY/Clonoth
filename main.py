"""Clonoth launcher.

默认启动 Supervisor，并自动拉起统一引擎进程与本地 CLI。

用法：
    python main.py

也可以单独启动：
    python -m supervisor.main
    python -m engine
"""

from __future__ import annotations

from supervisor.main import main


if __name__ == "__main__":
    main()

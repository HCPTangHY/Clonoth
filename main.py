"""Clonoth launcher.

默认启动 Supervisor（包含 Gateway API），并自动拉起 Shell CLI 与 Kernel Worker。

用法：
    python main.py

也可以单独启动：
    python -m supervisor.main
    python -m shell.worker
    python -m kernel.worker
"""

from __future__ import annotations

from supervisor.main import main


if __name__ == "__main__":
    main()

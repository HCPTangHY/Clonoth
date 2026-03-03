from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from clonoth_runtime import get_bool, get_float, load_runtime_config


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


@dataclass
class ManagedProcess:
    name: str
    popen: subprocess.Popen
    log_path: Path | None


class ProcessManager:
    """Spawn/restart shell & kernel workers.

    说明：
    - 这是“控制面”能力的一部分：即使 shell 自进化，把自己搞挂，Supervisor 也能重启/回滚。
    - MVP 使用 subprocess；后续可加入健康检查、重启风暴保护等。
    """

    def __init__(self, *, supervisor_url: str, workspace_root: Path, log_dir: Path) -> None:
        self.supervisor_url = supervisor_url
        self.workspace_root = workspace_root
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Shell Orchestrator Worker (headless)
        self.shell: ManagedProcess | None = None
        # Optional interactive CLI adapter (spawns in a separate console on Windows)
        self.shell_cli: ManagedProcess | None = None
        self.kernel: ManagedProcess | None = None

        runtime_cfg = load_runtime_config(workspace_root)

        self.stop_wait_timeout_sec = get_float(
            runtime_cfg,
            "supervisor.process_manager.stop_wait_timeout_sec",
            5.0,
            min_value=0.1,
            max_value=60.0,
        )

        # Windows 下让交互式 Shell 使用独立控制台，避免与 Supervisor(Uvicorn) 日志抢占同一终端。
        platform_default_new_console = True if os.name == "nt" else False
        cfg_new_console = get_bool(runtime_cfg, "supervisor.process_manager.shell_new_console", None)
        shell_new_console = cfg_new_console if cfg_new_console is not None else platform_default_new_console

        # Env override (kept for convenience)
        env_raw = os.getenv("CLONOTH_SHELL_NEW_CONSOLE")
        if env_raw is not None and env_raw.strip():
            shell_new_console = env_raw.strip().lower() in {"1", "true", "yes", "y"}

        self.shell_new_console = bool(shell_new_console)

        # Whether to spawn an interactive CLI adapter alongside the orchestrator worker.
        # The CLI is useful for local development and for handling approvals.
        self.spawn_shell_cli = os.getenv("CLONOTH_SPAWN_SHELL_CLI", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
        }

    def _spawn(
        self,
        *,
        name: str,
        module: str,
        extra_args: list[str],
        capture_output: bool,
        new_console: bool,
    ) -> ManagedProcess:
        log_path: Path | None = None
        stdout = None
        stderr = None

        if capture_output:
            log_path = self.log_dir / f"{name}-{_timestamp()}.log"
            log_f = log_path.open("a", encoding="utf-8")
            stdout = log_f
            stderr = subprocess.STDOUT

        env = os.environ.copy()
        env["CLONOTH_SUPERVISOR_URL"] = self.supervisor_url

        cmd = [sys.executable, "-m", module, "--supervisor", self.supervisor_url, *extra_args]

        creationflags = 0
        if new_console and os.name == "nt":
            # type: ignore[attr-defined]
            creationflags |= subprocess.CREATE_NEW_CONSOLE

        p = subprocess.Popen(
            cmd,
            cwd=str(self.workspace_root),
            stdout=stdout,
            stderr=stderr,
            env=env,
            creationflags=creationflags,
        )
        return ManagedProcess(name=name, popen=p, log_path=log_path)

    def start_shell(self) -> None:
        # Start shell orchestrator worker (headless)
        if self.shell and self.shell.popen.poll() is None:
            # still optionally ensure cli exists
            if self.spawn_shell_cli:
                self.start_shell_cli()
            return

        self.shell = self._spawn(
            name="shell",
            module="shell.worker",
            extra_args=[],
            capture_output=True,
            new_console=False,
        )

        if self.spawn_shell_cli:
            self.start_shell_cli()

    def start_shell_cli(self) -> None:
        if self.shell_cli and self.shell_cli.popen.poll() is None:
            return
        # CLI 需要交互；Windows 默认使用独立控制台。
        self.shell_cli = self._spawn(
            name="shell-cli",
            module="shell.cli",
            extra_args=[],
            capture_output=False,
            new_console=self.shell_new_console,
        )

    def start_kernel(self) -> None:
        if self.kernel and self.kernel.popen.poll() is None:
            return
        # Kernel 默认写日志到文件，避免打乱 shell 的交互体验。
        self.kernel = self._spawn(
            name="kernel",
            module="kernel.worker",
            extra_args=[],
            capture_output=True,
            new_console=False,
        )

    def stop_shell(self) -> None:
        if self.shell_cli:
            self._stop(self.shell_cli)
            self.shell_cli = None

        if self.shell:
            self._stop(self.shell)
            self.shell = None

    def stop_kernel(self) -> None:
        if not self.kernel:
            return
        self._stop(self.kernel)
        self.kernel = None

    def restart_shell(self) -> None:
        self.stop_shell()
        self.start_shell()

    def restart_kernel(self) -> None:
        self.stop_kernel()
        self.start_kernel()

    def stop_all(self) -> None:
        self.stop_shell()
        self.stop_kernel()

    def _stop(self, proc: ManagedProcess) -> None:
        p = proc.popen
        if p.poll() is not None:
            return

        try:
            p.terminate()
            p.wait(timeout=float(self.stop_wait_timeout_sec))
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

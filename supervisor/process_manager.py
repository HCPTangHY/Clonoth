from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from clonoth_runtime import get_bool, get_float, get_int, load_runtime_config


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


@dataclass
class ManagedProcess:
    name: str
    popen: subprocess.Popen
    log_path: Path | None


class ProcessManager:
    """管理引擎 worker 和 CLI 适配器进程。"""

    def __init__(self, *, supervisor_url: str, workspace_root: Path, log_dir: Path) -> None:
        self.supervisor_url = supervisor_url
        self.workspace_root = workspace_root
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.engines: list[ManagedProcess] = []
        self.shell_cli: ManagedProcess | None = None

        runtime_cfg = load_runtime_config(workspace_root)

        self.stop_wait_timeout_sec = get_float(
            runtime_cfg,
            "supervisor.process_manager.stop_wait_timeout_sec",
            5.0,
            min_value=0.1,
            max_value=30.0,
        )
        self.engine_workers = get_int(
            runtime_cfg,
            "supervisor.process_manager.engine_workers",
            2,
            min_value=1,
            max_value=8,
        )

        platform_default_new_console = sys.platform == "win32"
        cfg_new_console = runtime_cfg.get("supervisor", {}).get("process_manager", {}).get("shell_new_console", None)
        shell_new_console = get_bool(runtime_cfg, "supervisor.process_manager.shell_new_console", platform_default_new_console)
        if cfg_new_console is None:
            shell_new_console = platform_default_new_console

        env_raw = os.getenv("CLONOTH_SHELL_NEW_CONSOLE", "").strip().lower()
        if env_raw in {"1", "true", "yes"}:
            shell_new_console = True
        elif env_raw in {"0", "false", "no"}:
            shell_new_console = False

        self.shell_new_console = shell_new_console
        self.spawn_shell_cli = get_bool(runtime_cfg, "supervisor.process_manager.spawn_shell_cli", True)

    def _spawn(
        self,
        *,
        name: str,
        module: str,
        extra_args: list[str] | None = None,
        capture_output: bool = True,
        new_console: bool = False,
    ) -> ManagedProcess:
        log_path = self.log_dir / f"{name}-{_timestamp()}.log" if capture_output else None
        stdout = None
        stderr = None
        if capture_output and log_path:
            log_f = log_path.open("a", encoding="utf-8")
            stdout = log_f
            stderr = subprocess.STDOUT

        env = {**os.environ, "CLONOTH_SUPERVISOR_URL": self.supervisor_url}

        cmd = [sys.executable, "-m", module, "--supervisor", self.supervisor_url]
        if extra_args:
            cmd.extend(extra_args)

        creationflags = 0
        if sys.platform == "win32" and new_console:
            creationflags = subprocess.CREATE_NEW_CONSOLE

        p = subprocess.Popen(
            cmd,
            cwd=str(self.workspace_root),
            env=env,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
        )
        print(f"[process_manager] spawned {name} pid={p.pid}", flush=True)
        return ManagedProcess(name=name, popen=p, log_path=log_path)

    def start_engine(self) -> None:
        self.engines = [proc for proc in self.engines if proc.popen.poll() is None]
        while len(self.engines) < self.engine_workers:
            idx = len(self.engines) + 1
            name = f"engine-{idx}"
            proc = self._spawn(
                name=name,
                module="engine",
                capture_output=True,
                new_console=False,
                extra_args=["--worker-id", name],
            )
            self.engines.append(proc)

    def start_shell_cli(self) -> None:
        if self.shell_cli and self.shell_cli.popen.poll() is None:
            return
        self.shell_cli = self._spawn(
            name="shell-cli",
            module="shell.cli",
            capture_output=False,
            new_console=self.shell_new_console,
        )

    def stop_engine(self) -> None:
        for proc in list(self.engines):
            self._stop(proc)
        self.engines = []

    def stop_shell_cli(self) -> None:
        if self.shell_cli:
            self._stop(self.shell_cli)
            self.shell_cli = None

    def restart_engine(self) -> None:
        self.stop_engine()
        self.start_engine()

    def stop_all(self) -> None:
        self.stop_shell_cli()
        self.stop_engine()

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

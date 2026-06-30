from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from clonoth_runtime import get_float, get_int, load_runtime_config


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _make_child_preexec():
    """返回 preexec_fn：Linux 上设置 PR_SET_PDEATHSIG，父进程退出时子进程自动收到 SIGTERM。"""
    if sys.platform == "linux":
        def _set_pdeathsig():
            try:
                import ctypes
                import ctypes.util
                libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
                PR_SET_PDEATHSIG = 1
                libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
            except Exception:
                pass
        return _set_pdeathsig
    return None


@dataclass
class ManagedProcess:
    name: str
    popen: subprocess.Popen
    log_path: Path | None


class ProcessManager:
    """管理引擎 worker 进程。"""

    def __init__(self, *, supervisor_url: str, workspace_root: Path, log_dir: Path, log_func=None) -> None:
        self.supervisor_url = supervisor_url
        self.workspace_root = workspace_root
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.engines: list[ManagedProcess] = []
        self._stopped = False
        self._restart_pending = False
        self._restarting_engine = False  # restart engine 期间抑制信号退出

        # 日志函数：写日志文件而不是终端。
        self._log = log_func or (lambda msg: print(msg))

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

        # 注册清理：supervisor 退出时杀掉所有子进程
        atexit.register(self._cleanup)
        self._install_signal_handlers()

    def _install_signal_handlers(self) -> None:
        """拦截 SIGTERM/SIGINT，先清理子进程再退出。"""
        for sig in (signal.SIGTERM, signal.SIGINT):
            prev_handler = signal.getsignal(sig)

            def _handler(signum, frame, _prev=prev_handler):
                import traceback as _tb
                try:
                    _sname = signal.Signals(signum).name
                except (ValueError, AttributeError):
                    _sname = str(signum)
                _tname = threading.current_thread().name
                # restart engine 期间信号泄漏到 supervisor，忽略不退出
                if self._restarting_engine:
                    return
                self._cleanup()
                # 调用原有的 handler
                if callable(_prev) and _prev not in (signal.SIG_IGN, signal.SIG_DFL):
                    _prev(signum, frame)
                else:
                    sys.exit(0)

            try:
                signal.signal(sig, _handler)
            except (OSError, ValueError):
                # 非主线程中无法设置信号处理
                pass

    def _cleanup(self) -> None:
        """清理所有子进程。可重入安全。"""
        if self._stopped:
            return
        self._stopped = True
        self._log("[process_manager] cleaning up child processes...")
        self.stop_all()

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
        preexec_fn = None
        if sys.platform == "win32" and new_console:
            creationflags = subprocess.CREATE_NEW_CONSOLE
        elif sys.platform != "win32":
            # Linux/macOS: 父进程退出时子进程自动收到 SIGTERM
            preexec_fn = _make_child_preexec()

        p = subprocess.Popen(
            cmd,
            cwd=str(self.workspace_root),
            env=env,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
            preexec_fn=preexec_fn,
            start_new_session=True,  # engine 独立进程组，杀 engine 不波及 supervisor
        )
        self._log(f"[process_manager] spawned {name} pid={p.pid}")
        return ManagedProcess(name=name, popen=p, log_path=log_path)

    def start_engine(self) -> None:
        self._stopped = False
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

    def stop_engine(self) -> None:
        for proc in list(self.engines):
            self._stop(proc)
        self.engines = []

    def restart_engine(self) -> None:
        self.stop_engine()
        self.start_engine()

    def stop_all(self) -> None:
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
                p.wait(timeout=5)  # reap after SIGKILL to release ports
            except Exception:
                pass
        self._log(f"[process_manager] stopped {proc.name} pid={p.pid}")

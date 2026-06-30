"""SignalBus — 进程内信号总线。

支持精确匹配和 glob 通配符订阅（如 'llm.*'）。
提供 span 上下文管理器，自动发送 name.start / name.end 配对信号。
线程安全（通过 threading.Lock 保护订阅者列表）。

全局单例通过 get_bus() 获取。
"""

from __future__ import annotations

import fnmatch
import logging
import threading
import time
from contextlib import contextmanager
from typing import Callable, Dict, List, Optional, Any

from engine.signals.types import Signal, make_span_id

log = logging.getLogger(__name__)

# 信号处理函数的类型：接收一个 Signal，无返回值
SignalHandler = Callable[[Signal], None]


class SignalBus:
    """进程内同步信号总线。

    设计要点:
    - emit 是同步的，handler 在调用方线程中依次执行。
      这保证了信号处理的顺序性和简单性。
    - handler 抛出的异常会被捕获并记录日志，不会影响后续 handler。
    - 支持 glob 通配符订阅：'llm.*' 匹配 'llm.call.start'、'llm.retry' 等。
    """

    def __init__(self) -> None:
        # _exact: 精确匹配的订阅者 {signal_name: [handler, ...]}
        self._exact: Dict[str, List[SignalHandler]] = {}
        # _glob: 通配符订阅者 [(pattern, handler), ...]
        self._glob: List[tuple] = []
        self._lock = threading.Lock()
        # enabled 开关：为 False 时 emit 直接跳过，零开销
        self.enabled: bool = True

    def subscribe(self, pattern: str, handler: SignalHandler) -> None:
        """订阅信号。

        Args:
            pattern: 信号名称或 glob 通配符（如 'llm.*', 'tool.call.*'）。
                     不含 '*' 和 '?' 时按精确匹配注册，否则按 glob 注册。
            handler: 接收 Signal 的回调函数。
        """
        with self._lock:
            # 判断是否包含通配符
            if "*" in pattern or "?" in pattern:
                self._glob.append((pattern, handler))
            else:
                self._exact.setdefault(pattern, []).append(handler)

    def emit(self, signal: Signal) -> None:
        """发送信号，同步调用所有匹配的 handler。

        如果 bus 被禁用（enabled=False），直接返回。
        handler 异常会被捕获记录，不会中断后续 handler 的执行。
        """
        if not self.enabled:
            return

        # 收集匹配的 handler（快照，避免持锁期间执行 handler）
        handlers: List[SignalHandler] = []
        with self._lock:
            # 精确匹配
            exact_handlers = self._exact.get(signal.name)
            if exact_handlers:
                handlers.extend(exact_handlers)
            # glob 匹配
            for pattern, handler in self._glob:
                if fnmatch.fnmatch(signal.name, pattern):
                    handlers.append(handler)

        # 在锁外执行 handler，避免死锁
        for handler in handlers:
            try:
                handler(signal)
            except Exception:
                log.exception(
                    "Signal handler %s raised for signal %s",
                    getattr(handler, "__name__", repr(handler)),
                    signal.name,
                )

    @contextmanager
    def span(self, name: str, payload: Optional[Dict[str, Any]] = None,
             trace_id: Optional[str] = None):
        """上下文管理器：自动发送 name.start 和 name.end 信号对。

        用法:
            with bus.span('llm.call', payload={'model': 'gpt-4'}) as span_id:
                result = await do_llm_call()

        start 信号在进入时发送，end 信号在退出时发送（无论是否异常）。
        end 信号的 payload 会包含 elapsed_ms 和 error（如有异常）。

        Args:
            name: 信号基础名称，会自动加 .start / .end 后缀。
            payload: start 信号携带的数据。
            trace_id: 追踪 ID，会注入到 start 和 end 信号。

        Yields:
            span_id: 本次 span 的唯一 ID，可用于关联其他信号。
        """
        sid = make_span_id()
        base_payload = payload or {}
        t0 = time.time()

        # 发送 start 信号
        self.emit(Signal(
            name=f"{name}.start",
            payload=base_payload,
            trace_id=trace_id,
            span_id=sid,
        ))

        error: Optional[Exception] = None
        try:
            yield sid
        except Exception as exc:
            error = exc
            raise
        finally:
            # 发送 end 信号，包含耗时和错误信息
            elapsed_ms = (time.time() - t0) * 1000
            end_payload = {
                **base_payload,
                "elapsed_ms": round(elapsed_ms, 1),
            }
            if error is not None:
                end_payload["error"] = str(error)
                end_payload["error_type"] = type(error).__name__
            self.emit(Signal(
                name=f"{name}.end",
                payload=end_payload,
                trace_id=trace_id,
                span_id=sid,
            ))


# ── 全局单例 ──────────────────────────────────────────────
# 使用模块级单例，通过 get_bus() 访问。
# 这样所有模块拿到的都是同一个 bus 实例。
_bus: Optional[SignalBus] = None
_bus_lock = threading.Lock()


def get_bus() -> SignalBus:
    """获取全局 SignalBus 单例。首次调用时自动创建。"""
    global _bus
    if _bus is None:
        with _bus_lock:
            if _bus is None:
                _bus = SignalBus()
    return _bus

"""Clonoth Signal System — Phase 0 基础设施。

提供轻量级进程内信号总线，用于解耦 engine 内部组件的事件通知。
设计文档见 data/signal_system_design.md。

用法:
    from engine.signals import Signal, SignalBus, get_bus
    bus = get_bus()
    bus.emit(Signal(name="llm.call.start", payload={...}))
"""

from engine.signals.types import Signal
from engine.signals.bus import SignalBus, get_bus

__all__ = ["Signal", "SignalBus", "get_bus"]

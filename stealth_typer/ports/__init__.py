"""Outbound port interfaces, ported from ``src/ports/outbound``."""

from .i_clipboard_port import IClipboardPort
from .i_event_bus import IEventBus, StatusChanged
from .i_input_injection_port import IInputInjectionPort
from .i_target_detector import ITargetDetector
from .i_timer import ITimer

__all__ = [
    "IClipboardPort",
    "IEventBus",
    "IInputInjectionPort",
    "ITargetDetector",
    "ITimer",
    "StatusChanged",
]

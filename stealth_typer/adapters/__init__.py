"""Concrete adapters. Windows ones need ``ctypes`` only; no extra packages."""

from .event_bus import ConsoleEventBus
from .thread_timer import ThreadTimer
from .disabled_input_port import DisabledInputInjectionPort
from .dry_run_input_port import DryRunInputInjectionPort
from .hotkey_listener import (
    AUTO_ARM_CANDIDATES,
    AUTO_PAUSE_CANDIDATES,
    HotkeyError,
    HotkeyListener,
    parse_hotkey,
    pick_free_hotkey,
    probe_hotkey,
)
from .simulated_target_detector import SimulatedTargetDetector
from .win_input_injection_adapter import WinInputInjectionAdapter
from .win_clipboard_adapter import WinClipboardAdapter
from .win_target_detector import WinTargetDetector

__all__ = [
    "ConsoleEventBus",
    "ThreadTimer",
    "DisabledInputInjectionPort",
    "DryRunInputInjectionPort",
    "HotkeyListener",
    "HotkeyError",
    "parse_hotkey",
    "probe_hotkey",
    "pick_free_hotkey",
    "AUTO_ARM_CANDIDATES",
    "AUTO_PAUSE_CANDIDATES",
    "SimulatedTargetDetector",
    "WinInputInjectionAdapter",
    "WinClipboardAdapter",
    "WinTargetDetector",
]

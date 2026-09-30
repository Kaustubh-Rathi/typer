"""Win32 ``SendInput`` keyboard injection, ported from
``src/adapters/input/input_injection_adapter.cpp``.

Uses ``ctypes`` so no third-party package is needed. Note that StealthDesk's
shipped binary deliberately drives the Interception driver instead of
``SendInput``; this standalone port keeps the app's ``SendInput`` call shape so
it runs on a stock Windows machine with no driver installed.
"""

import ctypes
from ctypes import wintypes

from ..core.utf8_utils import codepoint_to_surrogates
from ..ports.i_input_injection_port import IInputInjectionPort

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

ERROR_INVALID_DATA = 13
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


def _keyboard_input(vk=0, scan=0, flags=0) -> INPUT:
    item = INPUT(type=INPUT_KEYBOARD)
    item.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return item


class WinInputInjectionAdapter(IInputInjectionPort):
    def __init__(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._last_error = 0

    def _send(self, inputs) -> bool:
        count = len(inputs)
        array = (INPUT * count)(*inputs)
        sent = self._user32.SendInput(count, ctypes.byref(array), ctypes.sizeof(INPUT))
        if sent != count:
            self._last_error = ctypes.get_last_error() or ERROR_INVALID_DATA
            return False
        self._last_error = 0
        return True

    def inject_virtual_key(self, vk: int, key_up: bool) -> bool:
        flags = KEYEVENTF_KEYUP if key_up else 0
        return self._send([_keyboard_input(vk=vk, flags=flags)])

    def inject_unicode_char(self, ch: int) -> bool:
        if ch <= 0xFFFF:
            down = _keyboard_input(scan=ch, flags=KEYEVENTF_UNICODE)
            up = _keyboard_input(scan=ch, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
            return self._send([down, up])

        # Supplementary plane character (> 0xFFFF, e.g. emoji): surrogate pair.
        high, low = codepoint_to_surrogates(ch)
        return self._send(
            [
                _keyboard_input(scan=high, flags=KEYEVENTF_UNICODE),
                _keyboard_input(scan=low, flags=KEYEVENTF_UNICODE),
                _keyboard_input(scan=high, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
                _keyboard_input(scan=low, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
            ]
        )

    def get_last_error(self) -> int:
        return self._last_error

    def is_available(self) -> bool:
        return True

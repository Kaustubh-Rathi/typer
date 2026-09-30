"""Port of StealthDesk ``src/core/typing_policy.h``.

Clamp typing speed to the app's supported range and derive the auto-type
timer interval from it. Speed 1 is roughly 500 ms per character, speed 100
roughly 5 ms.
"""

from enum import Enum


class AutoIndentMode(Enum):
    """Port of ``stealthdesk::AutoIndentMode``, plus an AUTO extension.

    RAW      Notepad mode: preserve source spaces and tabs exactly
    SMART_IDE Smart IDE mode: skip redundant leading indentation after \\n
    AUTO     Resolve to RAW or SMART_IDE from the target window (this port's
             addition; the C++ original has no third mode)
    """

    RAW = "raw"
    SMART_IDE = "smart"
    AUTO = "auto"


def clamp_speed(speed: int) -> int:
    """Clamp typing speed to the app's supported range [1, 100]."""
    return max(1, min(100, int(speed)))


def typing_interval_ms(speed: int) -> int:
    """Timer interval for auto-type. Speed 1 ~ 500 ms, speed 100 ~ 5 ms."""
    s = clamp_speed(speed)
    return max(5, min(500, 505 - (s * 5)))

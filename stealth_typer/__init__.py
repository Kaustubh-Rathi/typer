"""Python port of StealthDesk's auto-typing engine ("typer").

Layering mirrors the original C++ project:

* ``core``    - pure policy: speed clamp, interval, UTF-8 helpers, session FSM,
                timing models (fixed and human-like)
* ``domain``  - ``TyperState``
* ``ports``   - outbound interfaces (input, clipboard, timer, event bus,
                target detector)
* ``adapters``- Win32 implementations plus disabled/dry-run stand-ins
* ``application`` - ``TyperService``, the state machine that drives it all
"""

from .application.typer_service import TyperService
from .core.timing_model import HumanizationLevel, HumanLikeTimingModel, FixedTimingModel
from .core.typing_policy import AutoIndentMode, clamp_speed, typing_interval_ms
from .core.typing_session import TypingSession
from .domain.typer_state import TyperState

__all__ = [
    "TyperService",
    "TyperState",
    "TypingSession",
    "HumanizationLevel",
    "HumanLikeTimingModel",
    "FixedTimingModel",
    "AutoIndentMode",
    "clamp_speed",
    "typing_interval_ms",
]

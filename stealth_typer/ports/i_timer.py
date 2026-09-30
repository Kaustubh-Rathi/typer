"""Port of ``src/ports/outbound/i_timer.h``.

Timers are identified by an integer id so a later ``start_*`` on the same id
replaces the pending callback, and ``stop`` cancels it. The typer uses three
ids: 200 (per-character tick), 201 (target monitor) and 202 (activation
settling).
"""

from abc import ABC, abstractmethod
from typing import Callable


class ITimer(ABC):
    @abstractmethod
    def start_periodic(self, timer_id: int, interval_ms: int, callback: Callable[[], None]) -> None:
        ...

    @abstractmethod
    def start_once(self, timer_id: int, delay_ms: int, callback: Callable[[], None]) -> None:
        ...

    @abstractmethod
    def stop(self, timer_id: int) -> None:
        ...

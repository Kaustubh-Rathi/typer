"""Thread-based ``ITimer``, replacing the C++ ``WinTimer``/UI dispatcher.

A single daemon thread services all timer ids, so the three typer timers
(per-character tick, target monitor, activation settling) never block each other.
"""

import threading
import time
from typing import Callable, Dict

from ..ports.i_timer import ITimer

Callback = Callable[[], None]


class _Entry:
    __slots__ = ("callback", "interval_s", "next_fire", "period")

    def __init__(self, callback: Callback, interval_s: float, next_fire: float, period: bool):
        self.callback = callback
        self.interval_s = interval_s
        self.next_fire = next_fire
        self.period = period


class ThreadTimer(ITimer):
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._entries: Dict[int, _Entry] = {}
        self._running = True
        self._thread = threading.Thread(target=self._run, name="typer-timer", daemon=True)
        self._thread.start()

    # -- ITimer ------------------------------------------------------------
    def start_periodic(self, timer_id: int, interval_ms: int, callback: Callback) -> None:
        self._arm(timer_id, interval_ms, callback, period=True)

    def start_once(self, timer_id: int, delay_ms: int, callback: Callback) -> None:
        self._arm(timer_id, delay_ms, callback, period=False)

    def stop(self, timer_id: int) -> None:
        with self._cv:
            self._entries.pop(timer_id, None)
            self._cv.notify_all()

    def shutdown(self) -> None:
        """Stop the thread; safe to call more than once."""
        with self._cv:
            self._running = False
            self._entries.clear()
            self._cv.notify_all()
        self._thread.join(timeout=1.0)

    # -- internals ---------------------------------------------------------
    def _arm(self, timer_id: int, delay_ms: int, callback: Callback, period: bool) -> None:
        interval_s = max(0.0, float(delay_ms) / 1000.0)
        with self._cv:
            self._entries[timer_id] = _Entry(
                callback, interval_s, time.monotonic() + interval_s, period
            )
            self._cv.notify_all()

    def _run(self) -> None:
        while True:
            callbacks = []
            with self._cv:
                if not self._running:
                    return
                if not self._entries:
                    self._cv.wait(0.1)
                    continue

                now = time.monotonic()
                due = [e for e in self._entries.values() if e.next_fire <= now]
                if not due:
                    soonest = min(e.next_fire for e in self._entries.values())
                    self._cv.wait(max(0.001, soonest - now))
                    continue

                for entry in due:
                    if entry.period:
                        entry.next_fire = now + entry.interval_s
                    else:
                        for tid, e in list(self._entries.items()):
                            if e is entry:
                                self._entries.pop(tid, None)
                    callbacks.append(entry.callback)
                self._cv.notify_all()

            # Callbacks run outside the lock: they re-arm their own timer, which
            # takes the lock, so holding it would deadlock.
            for callback in callbacks:
                try:
                    callback()
                except Exception:  # a timer callback must never kill the thread
                    pass

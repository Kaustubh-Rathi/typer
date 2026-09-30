"""Port of ``src/ports/outbound/i_event_bus.h`` (status channel only).

The C++ event bus carries many event kinds; the typer only publishes status
text, so this port keeps just ``StatusChanged``.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, List


@dataclass(frozen=True)
class StatusChanged:
    text: str


StatusListener = Callable[[StatusChanged], None]


class IEventBus(ABC):
    @abstractmethod
    def on_status(self, listener: StatusListener) -> None:
        ...

    @abstractmethod
    def publish_status(self, event: StatusChanged) -> None:
        ...


class CollectingEventBus(IEventBus):
    """In-memory bus that keeps every status; handy for tests and dry runs."""

    def __init__(self) -> None:
        self._listeners: List[StatusListener] = []
        self.statuses: List[str] = []

    def on_status(self, listener: StatusListener) -> None:
        self._listeners.append(listener)

    def publish_status(self, event: StatusChanged) -> None:
        self.statuses.append(event.text)
        for listener in list(self._listeners):
            listener(event)

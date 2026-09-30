"""Event bus that prints every status to stdout, like the app's status line."""

import sys

from ..ports.i_event_bus import CollectingEventBus, StatusChanged


class ConsoleEventBus(CollectingEventBus):
    """Event bus that prints every status to stdout, like the app's status line."""

    def __init__(self, prefix: str = "[typer]", echo: bool = True, stream=None) -> None:
        super().__init__()
        self._prefix = prefix
        self._echo = echo
        self._stream = stream or sys.stdout

    def publish_status(self, event: StatusChanged) -> None:
        super().publish_status(event)
        if self._echo:
            try:
                self._stream.write("%s %s\n" % (self._prefix, event.text))
                self._stream.flush()
            except Exception:
                pass

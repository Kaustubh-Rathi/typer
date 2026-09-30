"""Port of ``src/ports/outbound/i_clipboard_port.h``.

``set_image`` is omitted: image buffers belong to the capture/OCR pipeline, not
to the typer, and the typer only ever reads text.
"""

from abc import ABC, abstractmethod


class IClipboardPort(ABC):
    @abstractmethod
    def set_text(self, text: str) -> None:
        ...

    @abstractmethod
    def get_text(self) -> str:
        ...

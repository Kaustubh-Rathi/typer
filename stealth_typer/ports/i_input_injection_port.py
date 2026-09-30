"""Port of ``src/ports/outbound/i_input_injection_port.h``."""

from abc import ABC, abstractmethod
from typing import Optional


class IInputInjectionPort(ABC):
    @abstractmethod
    def inject_virtual_key(self, vk: int, key_up: bool) -> bool:
        ...

    @abstractmethod
    def inject_unicode_char(self, ch: int) -> bool:
        ...

    def get_last_error(self) -> int:
        return 0

    def is_available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    def set_keyboard_suppression(self, enabled: bool) -> bool:
        return False

    def keyboard_suppression_active(self) -> bool:
        return False

    def inject_absolute_click(self, x: int, y: int) -> bool:
        return False

    def inject_submit_key(self) -> bool:
        return False

    def _reason_or_default(self, default: Optional[str]) -> str:
        why = self.unavailable_reason()
        return why if why else (default or "")

"""Port of StealthDesk ``src/application/services/i_typer_service.h``."""

from abc import ABC, abstractmethod

from ..core.timing_model import HumanizationLevel
from ..core.typing_policy import AutoIndentMode
from ..domain.typer_state import TyperState


class ITyperService(ABC):
    @abstractmethod
    def set_speed(self, speed: int) -> None:
        ...

    @property
    @abstractmethod
    def speed(self) -> int:
        ...

    @property
    @abstractmethod
    def state(self) -> TyperState:
        ...

    @property
    @abstractmethod
    def position(self) -> int:
        ...

    @property
    @abstractmethod
    def total_codepoints(self) -> int:
        ...

    @abstractmethod
    def arm(self) -> None:
        ...

    @abstractmethod
    def start_auto(self) -> None:
        ...

    @abstractmethod
    def pause_auto(self) -> None:
        ...

    @abstractmethod
    def reset(self) -> None:
        ...

    @abstractmethod
    def inject_vk(self, vk: int) -> None:
        ...

    @abstractmethod
    def set_target_hwnd(self, hwnd: int) -> None:
        ...

    @abstractmethod
    def set_stealthdesk_hwnd(self, hwnd: int) -> None:
        ...

    @abstractmethod
    def set_target_detector(self, detector) -> None:
        ...

    @abstractmethod
    def set_humanization_level(self, level: HumanizationLevel) -> None:
        ...

    @property
    @abstractmethod
    def humanization_level(self) -> HumanizationLevel:
        ...

    @abstractmethod
    def set_auto_indent_mode(self, mode: AutoIndentMode) -> None:
        ...

    @property
    @abstractmethod
    def auto_indent_mode(self) -> AutoIndentMode:
        ...

    @abstractmethod
    def set_activation_settling_delay_ms(self, ms: int) -> None:
        ...

    @property
    @abstractmethod
    def activation_settling_delay_ms(self) -> int:
        ...

    @abstractmethod
    def evaluate_target_window(self) -> None:
        ...

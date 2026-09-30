"""Port of ``src/ports/outbound/i_target_detector.h``.

Handles are plain ``int`` window handles (0 means "none"), matching the
``std::uintptr_t`` handles in the C++ ports.
"""

from abc import ABC, abstractmethod
from typing import Optional


class ITargetDetector(ABC):
    @abstractmethod
    def is_editable_target(self, target_handle: int, stealthdesk_handle: int) -> bool:
        ...

    @abstractmethod
    def is_stealthdesk_window(self, handle: int, stealthdesk_handle: int) -> bool:
        ...

    @abstractmethod
    def is_window_valid(self, handle: int) -> bool:
        ...

    @abstractmethod
    def get_foreground_window(self) -> int:
        ...

    def is_auto_indenting_editor(self, handle: int) -> Optional[bool]:
        """Whether this window inserts its own indentation after ENTER.

        ``True``  - the editor indents by itself, so leading whitespace should
                    be skipped (SmartIDE mode)
        ``False`` - it does not, so source indentation must be typed (Raw mode)
        ``None``  - unknown; the caller must pick the safe fallback

        Optional with a default so a detector that cannot answer (or a test
        double) is not forced to implement an operation it never uses.
        """
        return None

    def is_transient_overlay(self, handle: int) -> bool:
        """Whether ``handle`` is a shell/IME/tooltip window to ignore.

        These flash as foreground for a few milliseconds during window
        activation but never receive injected keystrokes. Treating them as a
        real focus change makes typing pause and resume in a loop.

        Defaults to ``False`` (never ignore) so a detector that knows nothing
        about overlays keeps its previous behaviour.
        """
        return False

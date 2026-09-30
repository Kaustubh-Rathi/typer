"""A target detector that invents an editable target, for safe testing.

Lets the whole arm -> settle -> type -> complete cycle run with no real window
involved, so the flow can be rehearsed before pointing it at an editor.
"""

from typing import Optional

from ..ports.i_target_detector import ITargetDetector

FAKE_TARGET = 0x1234  # any non-zero handle; never passed to Win32 here


class SimulatedTargetDetector(ITargetDetector):
    """Pretends a fixed handle is always an editable, auto-indenting editor."""

    def __init__(
        self,
        editable: bool = True,
        valid: bool = True,
        auto_indenting: Optional[bool] = True,
        handle: int = FAKE_TARGET,
    ) -> None:
        self.editable = editable
        self.valid = valid
        self.auto_indenting = auto_indenting
        self.handle = handle
        # Flip these mid-run to rehearse the safety paths.
        self.foreground = handle

    def is_editable_target(self, target_handle: int, stealthdesk_handle: int) -> bool:
        if not target_handle or target_handle == stealthdesk_handle:
            return False
        return self.editable

    def is_stealthdesk_window(self, handle: int, stealthdesk_handle: int) -> bool:
        return bool(handle) and handle == stealthdesk_handle

    def is_window_valid(self, handle: int) -> bool:
        return bool(handle) and self.valid

    def get_foreground_window(self) -> int:
        return self.foreground

    def is_auto_indenting_editor(self, handle: int) -> Optional[bool]:
        return self.auto_indenting

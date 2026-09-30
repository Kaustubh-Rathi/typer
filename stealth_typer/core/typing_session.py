"""Port of StealthDesk ``src/core/typing_session.h``.

A pure finite-state machine tracking progress through the Unicode codepoints
to be auto-typed. Position is tracked strictly in codepoints.
"""

from typing import List, Optional

from .utf8_utils import normalize_line_endings, utf8_to_codepoints


class TypingSession:
    def __init__(self, text: str = "") -> None:
        self._raw_text = ""
        self._normalized_text = ""
        self._codepoints: List[int] = []
        self._pos = 0
        if text:
            self.set_text(text)

    # -- loading -----------------------------------------------------------
    def set_text(self, text: str) -> None:
        self._raw_text = text
        self._normalized_text = normalize_line_endings(self._raw_text)
        self._codepoints = utf8_to_codepoints(self._normalized_text)
        self._pos = 0

    load_text = set_text

    # -- accessors ---------------------------------------------------------
    @property
    def raw_text(self) -> str:
        return self._raw_text

    @property
    def text(self) -> str:
        return self._normalized_text

    @property
    def codepoints(self) -> List[int]:
        return self._codepoints

    @property
    def position(self) -> int:
        return self._pos

    @property
    def total_codepoints(self) -> int:
        return len(self._codepoints)

    # -- position ----------------------------------------------------------
    def set_position(self, pos: int) -> None:
        self._pos = pos if pos <= len(self._codepoints) else len(self._codepoints)

    def reset(self) -> None:
        self._pos = 0

    def clear(self) -> None:
        self._raw_text = ""
        self._normalized_text = ""
        self._codepoints = []
        self._pos = 0

    # -- state -------------------------------------------------------------
    def is_complete(self) -> bool:
        return self._pos >= len(self._codepoints)

    def has_next(self) -> bool:
        return not self.is_complete()

    def peek_codepoint(self) -> Optional[int]:
        """Peek at the next Unicode codepoint without consuming it."""
        if not self.has_next():
            return None
        return self._codepoints[self._pos]

    def consume(self) -> bool:
        """Advance past the current codepoint. False if already complete."""
        if not self.has_next():
            return False
        self._pos += 1
        return True

    def advance_codepoint(self) -> Optional[int]:
        """Peek the next codepoint and consume it in one step."""
        cp = self.peek_codepoint()
        if cp is not None:
            self.consume()
        return cp

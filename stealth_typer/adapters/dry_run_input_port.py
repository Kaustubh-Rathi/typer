"""An input port that records what would be typed instead of typing it.

Lets you verify the whole session (spacing, delays, auto-indent skipping,
completion) without any risk to the machine, and is what ``--dry-run`` uses.
"""

from typing import List, Tuple

from ..core.utf8_utils import codepoint_to_utf8
from ..ports.i_input_injection_port import IInputInjectionPort

VK_RETURN = 0x0D
VK_TAB = 0x09


class DryRunInputInjectionPort(IInputInjectionPort):
    def __init__(self, echo: bool = True) -> None:
        self.unicode_chars: List[int] = []
        self.virtual_keys: List[int] = []
        # Every injection in the order it happened, so the transcript can be
        # reassembled exactly. Keeping a separate ordered log is what stops
        # newlines from all landing at the end of typed_text().
        self._events: List[Tuple[str, object]] = []
        self._echo = echo

    def inject_virtual_key(self, vk: int, key_up: bool) -> bool:
        # Only record the press, so the log reads as one key per action.
        if not key_up:
            self.virtual_keys.append(vk)
            self._events.append(("vk", vk))
            if self._echo:
                print("[dry-run] VK 0x%02X down" % vk, flush=True)
        return True

    def inject_unicode_char(self, ch: int) -> bool:
        self.unicode_chars.append(ch)
        self._events.append(("ch", ch))
        if self._echo:
            print("[dry-run] U+%04X %r" % (ch, codepoint_to_utf8(ch)), flush=True)
        return True

    def get_last_error(self) -> int:
        return 0

    def is_available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    def typed_text(self) -> str:
        """Everything that would have been delivered, in order, as one string."""
        parts: List[str] = []
        for kind, value in self._events:
            if kind == "ch":
                parts.append(codepoint_to_utf8(value))
            elif value == VK_RETURN:
                parts.append("\n")
            elif value == VK_TAB:
                parts.append("\t")
        return "".join(parts)

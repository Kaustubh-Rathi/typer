"""Port of ``src/adapters/input/disabled_input_port.h``.

An input port that can inject nothing and says why. Every method fails loudly
rather than pretending to work, and ``get_last_error`` returns
ERROR_NOT_SUPPORTED (50) so callers that map Win32 error codes report a real
reason rather than a bare "failed".

Use it with ``--no-input`` to rehearse the state machine without touching the
real keyboard.
"""

from ..ports.i_input_injection_port import IInputInjectionPort

ERROR_NOT_SUPPORTED = 50


class DisabledInputInjectionPort(IInputInjectionPort):
    REASON = "Input driver not installed - typing and clicking are disabled"

    def inject_virtual_key(self, vk: int, key_up: bool) -> bool:
        return False

    def inject_unicode_char(self, ch: int) -> bool:
        return False

    def get_last_error(self) -> int:
        return ERROR_NOT_SUPPORTED

    def is_available(self) -> bool:
        return False

    def unavailable_reason(self) -> str:
        return self.REASON

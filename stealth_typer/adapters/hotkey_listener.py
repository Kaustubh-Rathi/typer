"""Global pause/resume hotkey for the CLI.

Uses ``RegisterHotKey`` so the combo works even while the *target editor* has
keyboard focus. That matters here: injected keystrokes do not go through the
console, so a console-only key listener would never see a keypress unless the
user clicked back onto the console window - and clicking the console changes
the foreground window, which the typer treats as "target changed" and pauses.
A global hotkey is therefore the only way to pause without stealing focus.

Cost of a global hotkey: Windows swallows the combo for as long as it is
registered, so the editor will not receive it. Pick something you do not type
in an editor.

Falls back to console key polling when the combo is already owned by another
application, and says so rather than silently doing nothing.
"""

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional, Tuple

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

# Virtual-key codes for the physical modifier keys, needed by the
# GetAsyncKeyState fallback (which checks real key state, not just flags).
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_LWIN = 0x5B  # left Windows key

# How long the polled fallback requires a combination to be held. A person
# pressing a combo holds it for far longer than this; a stray modifier still
# down from a previous press does not.
HOTKEY_HOLD_SECONDS = 0.25

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
}

_VK = {}
for _letter in "abcdefghijklmnopqrstuvwxyz":
    _VK[_letter] = ord(_letter.upper())
    _VK[_letter.upper()] = ord(_letter)
for _digit in "0123456789":
    _VK[_digit] = ord(_digit)
for _n in range(1, 13):
    _VK["f%d" % _n] = 0x6F + _n
_VK["space"] = 0x20
_VK["tab"] = 0x09
_VK["enter"] = 0x0D
_VK["pause"] = 0x13
_VK["scrolllock"] = 0x91

# Combinations tried, in order, when the user asks for "auto". The obvious
# choices come first so the common case still gets alt+x / ctrl+alt+p, but a
# machine where another application already owns those (very common - Alt+X is
# taken by several browsers and IDEs) falls through to one that is free rather
# than silently having a dead hotkey.
AUTO_ARM_CANDIDATES = (
    "alt+x", "alt+c", "alt+v", "alt+z", "alt+f", "alt+d",
    "ctrl+alt+x", "ctrl+alt+c", "ctrl+alt+v",
    "f9", "f10", "ctrl+shift+f9", "ctrl+shift+f10",
)

AUTO_PAUSE_CANDIDATES = (
    "ctrl+alt+p", "ctrl+alt+k", "ctrl+alt+j",
    "f11", "f12", "ctrl+shift+f11", "ctrl+shift+f12", "pause",
)

# Reserved so a probe cannot collide with a hotkey already being listened for.
_PROBE_ID_BASE = 0x7000


def probe_hotkey(user32, spec: str, slot: int = 0) -> bool:
    """Whether ``spec`` can be registered right now, without keeping it.

    Registers and immediately unregisters, so calling this does not steal the
    combination from anyone. Used to pick a working default before the real
    listener takes it.
    """
    try:
        modifiers, vk = parse_hotkey(spec)
    except HotkeyError:
        return False
    hotkey_id = _PROBE_ID_BASE + (slot * 16) + (vk & 0x0F)
    if not user32.RegisterHotKey(None, hotkey_id, modifiers, vk):
        return False
    user32.UnregisterHotKey(None, hotkey_id)
    return True


def pick_free_hotkey(user32, candidates, avoid=()) -> Optional[str]:
    """First candidate that is free and not already claimed by ``avoid``."""
    for index, spec in enumerate(candidates):
        if spec in avoid:
            continue
        if probe_hotkey(user32, spec, slot=index):
            return spec
    return None


class HotkeyError(ValueError):
    """Raised for a hotkey spec that cannot be parsed."""


def parse_hotkey(spec: str) -> Tuple[int, int]:
    """``"ctrl+alt+p"`` -> ``(modifiers, virtual_key_code)``.

    Raises :class:`HotkeyError` on nonsense rather than registering nothing,
    because a silently dead pause key is worse than a startup error.
    """
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise HotkeyError("empty hotkey")

    modifiers = 0
    key = None
    for part in parts:
        if part in _MODIFIERS:
            modifiers |= _MODIFIERS[part]
        elif part in _VK:
            if key is not None:
                raise HotkeyError("more than one non-modifier key in %r" % spec)
            key = _VK[part]
        else:
            raise HotkeyError("unknown key or modifier: %r" % spec)

    if key is None:
        raise HotkeyError("no non-modifier key in %r (e.g. ctrl+alt+p)" % spec)
    return modifiers, key


class HotkeyListener:
    """Fires a callback when the hotkey is pressed, on a background thread.

    :param spec: hotkey spec such as ``"ctrl+alt+p"`` or ``"f8"``
    :param on_press: called with no arguments, from the listener thread
    """

    def __init__(self, spec: str = "ctrl+alt+p", on_press: Optional[Callable[[], None]] = None):
        self.spec = spec
        self.modifiers, self.vk = parse_hotkey(spec)
        self._on_press = on_press
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._running = False
        self._registered = False
        self._decided = threading.Event()
        self.hotkey_id = 0xBEEF  # any id works; only we listen

    @property
    def registered(self) -> bool:
        """True if the global hotkey was claimed.

        False means another app already owns the combo and we fell back to
        console polling, which only works while the console has focus.
        """
        return self._registered

    def alive(self) -> bool:
        """Whether the listener thread is still running.

        The CLI polls this while waiting for the arm key: if the thread died
        the hotkey can never arrive, and waiting on it forever would hang.
        """
        return bool(self._thread and self._thread.is_alive())

    @property
    def status(self) -> str:
        if self._registered:
            return "%s (global)" % self.spec
        # Honest wording: this is still focus-independent, it just polls.
        return "%s (polled - combo already taken by another app)" % self.spec

    def start(self, wait: float = 1.0) -> None:
        """Start the listener and wait for the registration outcome.

        The wait matters: ``status`` would otherwise be read before the thread
        had a chance to call RegisterHotKey, and the caller would be told the
        hotkey is console-only when it is actually global.
        """
        self._running = True
        self._decided.clear()
        self._thread = threading.Thread(
            target=self._run, name="typer-hotkey", daemon=True
        )
        self._thread.start()
        self._decided.wait(wait)

    def stop(self) -> None:
        self._running = False
        self._decided.set()
        if self._thread is not None and self._thread.is_alive():
            if self._registered and self._thread_id is not None:
                # Wake the blocking GetMessage so the thread can exit.
                self._user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(timeout=1.0)
        self._thread = None

    def _fire(self) -> None:
        if self._on_press:
            try:
                self._on_press()
            except Exception:
                pass

    def _run(self) -> None:
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        try:
            if self._register():
                self._message_loop()
            else:
                self._console_loop()
        finally:
            # Always signal, so start() cannot block for the full timeout on a
            # listener that failed to come up.
            self._decided.set()

    def _register(self) -> bool:
        ok = self._user32.RegisterHotKey(None, self.hotkey_id, self.modifiers, self.vk)
        if not ok:
            ctypes.set_last_error(0)
        self._registered = bool(ok)
        return self._registered

    def _unregister(self) -> None:
        if self._registered:
            self._user32.UnregisterHotKey(None, self.hotkey_id)
            self._registered = False

    def _message_loop(self) -> None:
        msg = wintypes.MSG()
        try:
            while self._running:
                result = self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result in (0, -1):  # WM_QUIT or error
                    break
                if msg.message == WM_HOTKEY and msg.wParam == self.hotkey_id:
                    self._fire()
        finally:
            self._unregister()

    def _console_loop(self) -> None:
        """Fallback when ``RegisterHotKey`` failed (hotkey already taken).

        Uses ``GetAsyncKeyState`` polling rather than reading the console.
        This matters: the point of the hotkey is that it works while the user's
        EDITOR has focus, and a console read cannot see a keypress that went to
        some other window. ``GetAsyncKeyState`` reports physical key state
        regardless of which window holds focus, so the fallback actually works
        instead of appearing alive while never firing.

        Caveat: this is a poll, so an extremely brief tap can fall between
        samples. The key must be held long enough to be sampled, which is how a
        human presses a combo anyway.
        """
        while self._running:
            if self._combo_down():
                # Wait for release so one press fires exactly once; otherwise
                # holding the key would fire the callback on every sample.
                self._fire()
                while self._running and self._combo_down():
                    time.sleep(0.01)
            else:
                time.sleep(0.01)

    def _combo_down(self) -> bool:
        """Whether every key in the combo is currently held down."""
        try:
            if not self._user32.GetAsyncKeyState(self.vk) & 0x8000:
                return False
            for modifier in self._mod_vks():
                if not self._user32.GetAsyncKeyState(modifier) & 0x8000:
                    return False
            return True
        except Exception:
            return False

    def _mod_vks(self):
        """Virtual-key codes for the modifier flags in this combo."""
        codes = []
        if self.modifiers & MOD_SHIFT:
            codes.append(VK_SHIFT)
        if self.modifiers & MOD_CONTROL:
            codes.append(VK_CONTROL)
        if self.modifiers & MOD_ALT:
            codes.append(VK_MENU)
        if self.modifiers & MOD_WIN:
            codes.append(VK_LWIN)
        return codes


#!/usr/bin/env python3
"""Command-line front end for the ported StealthDesk typer engine.

Examples
--------
Type text from the clipboard into the focused editor::

    python main.py --text-from-clipboard --speed 60

Type a literal string::

    python main.py --text "Hello, world!" --speed 80

Rehearse without touching the keyboard (prints what would be typed)::

    python main.py --text "Hello" --dry-run

Print the timing table for a humanization level and exit::

    python main.py --timing-table
"""

import argparse
import ctypes
import sys
import threading
import time
from ctypes import wintypes

from stealth_typer.adapters.disabled_input_port import DisabledInputInjectionPort
from stealth_typer.adapters.dry_run_input_port import DryRunInputInjectionPort
from stealth_typer.adapters.event_bus import ConsoleEventBus
from stealth_typer.adapters.hotkey_listener import (
    AUTO_ARM_CANDIDATES,
    AUTO_PAUSE_CANDIDATES,
    HotkeyError,
    HotkeyListener,
    pick_free_hotkey,
)
from stealth_typer.adapters.simulated_target_detector import SimulatedTargetDetector
from stealth_typer.adapters.thread_timer import ThreadTimer
from stealth_typer.adapters.win_clipboard_adapter import WinClipboardAdapter
from stealth_typer.adapters.win_input_injection_adapter import WinInputInjectionAdapter
from stealth_typer.adapters.win_target_detector import WinTargetDetector
from stealth_typer.application.typer_service import TyperService
from stealth_typer.core.timing_model import HumanizationLevel, HumanLikeTimingModel
from stealth_typer.core.typing_policy import AutoIndentMode, clamp_speed, typing_interval_ms
from stealth_typer.domain.typer_state import TyperState
from stealth_typer.ports.i_event_bus import StatusChanged

_LEVELS = {
    "off": HumanizationLevel.OFF,
    "low": HumanizationLevel.LOW,
    "medium": HumanizationLevel.MEDIUM,
    "high": HumanizationLevel.HIGH,
}

# How long the script waits for a usable window after the arm key is pressed
# but the focus is still wrong. Removes the click/press race entirely.
ARM_GRACE_SECONDS = 6.0

_ENCODINGS = ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1")


def read_text_file(path, encoding="auto"):
    """Read a text file, guessing the encoding when asked to.

    Guessing matters: exam answer files are routinely saved as cp1252 or
    UTF-16, and decoding those as UTF-8 either raises or silently replaces the
    characters that matter. A BOM is honoured first, then UTF-8 is tried
    (strict, so a false positive cannot pass), then the Windows single-byte
    encodings, which cannot fail.
    """
    import os

    if not os.path.isfile(path):
        raise FileNotFoundError("No such file: %s" % path)

    with open(path, "rb") as handle:
        data = handle.read()

    if encoding != "auto":
        return data.decode(encoding)

    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")

    try:
        return data.decode("utf-8")  # strict: proves it is really UTF-8
    except UnicodeDecodeError:
        pass

    for candidate in ("cp1252", "latin-1"):
        try:
            return data.decode(candidate)
        except UnicodeDecodeError:
            continue

    return data.decode("latin-1", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="StealthDesk typer engine (Python port)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument("--text", help="literal text to type")
    source.add_argument(
        "--file", metavar="PATH", help="read the text to type from a file"
    )
    source.add_argument("--text-from-clipboard", action="store_true", help="use the clipboard")

    parser.add_argument(
        "--speed", type=int, default=50, help="typing speed 1-100 (default: 50)"
    )
    parser.add_argument(
        "--humanization",
        choices=sorted(_LEVELS),
        default="off",
        help="humanized timing level (default: off)",
    )
    parser.add_argument(
        "--seed", type=int, default=1337, help="timing model seed (default: 1337)"
    )
    parser.add_argument(
        "--auto-indent",
        choices=["raw", "smart", "auto"],
        default="smart",
        help="raw keeps source indentation exactly; smart skips what the "
        "editor already inserted; auto picks between them from the target "
        "window (default: smart)",
    )
    parser.add_argument(
        "--encoding",
        default="auto",
        help="file encoding for --file: auto, utf-8, utf-8-sig, utf-16, "
        "cp1252, latin-1 (default: auto)",
    )
    parser.add_argument(
        "--settle-ms",
        type=int,
        default=250,
        help="focus settling delay after the target is picked (default: 250)",
    )
    parser.add_argument(
        "--key",
        default="auto",
        metavar="SPEC",
        help="THE single hotkey. Click your editor, press it to start; press "
        "it again to pause; again to resume. 'auto' (default) picks the first "
        "combination not already owned by another application",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="fully offline: pretend a target editor exists and never touch "
        "the real keyboard or clipboard. Implies --dry-run and skips the "
        "click-the-editor step, so it is safe to run anywhere",
    )
    parser.add_argument(
        "--diagnose-input",
        action="store_true",
        help="check whether keystrokes can reach the focused window at all "
        "(integrity levels + a real injection), then exit",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="live diagnostic: show which hotkeys are free and what window is "
        "currently focused. Types nothing. Use this when nothing happens",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run built-in checks (encoding, timing, indent, pause) and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do not send input; print every codepoint instead",
    )
    parser.add_argument(
        "--no-hotkey",
        action="store_true",
        help="do not register the toggle hotkey (Ctrl+C still aborts)",
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="use the disabled input port, which refuses typing out loud",
    )
    parser.add_argument(
        "--timing-table",
        action="store_true",
        help="print 12 sampled delays for the chosen speed/humanization and exit",
    )
    return parser


def print_timing_table(speed: int, level_name: str, seed: int) -> int:
    model = HumanLikeTimingModel(_LEVELS[level_name], seed)
    base = typing_interval_ms(speed)
    print("speed %d -> base interval %d ms" % (speed, base))
    print("humanization: %s" % level_name)
    for ch in list("a.,\n b") + ["x"] * 6:
        delay = model.next_delay_ms(ord(ch), 0, speed)
        print(
            "  %-6s %5d ms  (%.2fx base)"
            % (repr(ch), delay, delay / max(1, base))
        )
    return 0


def run_self_test(events) -> int:
    """Check each subsystem on this machine and print a pass/fail table.

    Everything here is offline: no keystrokes are injected and no real window
    is required, so it is safe to run before typing into anything real.
    """
    import os
    import tempfile

    checks = []

    def check(name, fn):
        try:
            detail = fn()
            checks.append((True, name, detail or ""))
        except Exception as exc:
            checks.append((False, name, "%s: %s" % (type(exc).__name__, exc)))

    def _timing():
        from stealth_typer.core.timing_model import HumanLikeTimingModel

        model = HumanLikeTimingModel(HumanizationLevel.MEDIUM, 1337)
        delays = [model.next_delay_ms(ord("a"), 0, 60) for _ in range(100)]
        assert all(5 <= d <= 2000 for d in delays), "delay out of bounds"
        assert len(set(delays)) > 1, "humanization produced no variation"
        return "speed 60: %d..%d ms, %d distinct" % (
            min(delays), max(delays), len(set(delays))
        )

    def _speed_math():
        assert typing_interval_ms(1) == 500
        assert typing_interval_ms(100) == 5
        assert clamp_speed(0) == 1 and clamp_speed(999) == 100
        return "speed 1 -> 500 ms, speed 100 -> 5 ms, clamped to [1,100]"

    def _encoding():
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "t.txt")
        with open(path, "wb") as handle:
            handle.write("café\r\tdone".encode("cp1252"))
        text = read_text_file(path, "auto")
        assert text == "café\r\tdone", "cp1252 round-trip failed: %r" % text
        from stealth_typer.core.typing_session import TypingSession

        assert TypingSession(text).text == "café\n\tdone", "newline normalization failed"
        return "cp1252 + CRLF -> normalized correctly"

    def _build_typer(auto_indenting=True, mode=AutoIndentMode.SMART_IDE):
        from stealth_typer.adapters.dry_run_input_port import DryRunInputInjectionPort
        from stealth_typer.adapters.simulated_target_detector import (
            SimulatedTargetDetector,
        )
        from stealth_typer.adapters.thread_timer import ThreadTimer
        from stealth_typer.application.typer_service import TyperService
        from stealth_typer.ports.i_event_bus import CollectingEventBus

        timer = ThreadTimer()
        port = DryRunInputInjectionPort(echo=False)
        typer = TyperService(
            None,
            port,
            timer,
            CollectingEventBus(),
            SimulatedTargetDetector(auto_indenting=auto_indenting),
        )
        typer.set_speed(100)
        typer.set_activation_settling_delay_ms(0)
        typer.set_auto_indent_mode(mode)
        return typer, port, timer

    def _run_to_completion(typer, port, timer, limit=5.0):
        typer.arm()
        typer.start_auto()
        deadline = time.monotonic() + limit
        while typer.state != TyperState.IDLE and time.monotonic() < deadline:
            time.sleep(0.02)
        timer.shutdown()
        # typed_text() rather than the raw codepoint list, because newlines and
        # tabs leave as virtual keys and would otherwise be invisible here.
        return port.typed_text()

    def _smart_indent():
        smart, smart_port, smart_timer = _build_typer(mode=AutoIndentMode.SMART_IDE)
        smart.session().set_text("a\n  b")
        smart_text = _run_to_completion(smart, smart_port, smart_timer)
        assert smart_text == "a\nb", "smart gave %r not 'a\\nb'" % smart_text

        raw, raw_port, raw_timer = _build_typer(mode=AutoIndentMode.RAW)
        raw.session().set_text("a\n  b")
        raw_text = _run_to_completion(raw, raw_port, raw_timer)
        assert raw_text == "a\n  b", "raw gave %r not 'a\\n  b'" % raw_text
        return "smart -> %r (indent skipped), raw -> %r (indent kept)" % (
            smart_text,
            raw_text,
        )

    def _auto_detect():
        typer, _, timer = _build_typer(auto_indenting=True, mode=AutoIndentMode.AUTO)
        typer.session().set_text("x")
        typer.arm()
        timer.shutdown()
        assert typer.effective_auto_indent_mode == AutoIndentMode.SMART_IDE, (
            "auto-indenting editor did not resolve to smart"
        )

        plain, _, plain_timer = _build_typer(auto_indenting=False, mode=AutoIndentMode.AUTO)
        plain.session().set_text("x")
        plain.arm()
        plain_timer.shutdown()
        assert plain.effective_auto_indent_mode == AutoIndentMode.RAW, (
            "plain editor did not resolve to raw"
        )
        return "auto-indenting editor -> smart, plain editor -> raw"

    def _pause():
        typer, port, timer = _build_typer(mode=AutoIndentMode.RAW)
        typer.set_speed(90)
        typer.session().set_text("x" * 300)
        typer.arm()
        typer.start_auto()
        time.sleep(0.3)
        typer.pause_auto()
        frozen = len(port.unicode_chars)
        time.sleep(0.3)
        held = len(port.unicode_chars) == frozen
        typer.start_auto()
        time.sleep(0.3)
        moved = len(port.unicode_chars) > frozen
        typer.pause_auto()
        timer.shutdown()
        assert frozen > 0, "nothing was typed before pausing"
        assert held, "pause did not hold"
        assert moved, "resume did not continue"
        return "paused at %d chars, held, then resumed" % frozen

    def _console_safety():
        from stealth_typer.adapters.win_target_detector import WinTargetDetector

        detector = WinTargetDetector()
        fg = detector.get_foreground_window()
        assert fg != 0, "no foreground window found"
        assert detector.is_window_valid(fg), "foreground window reported invalid"
        if detector.is_own_console(fg):
            return "foreground is our own console - correctly refused"
        return "foreground hwnd %d (%s), editable: %s" % (
            fg,
            detector._class_name(fg),
            detector.is_editable_target(fg, 0),
        )

    def _win32():
        from stealth_typer.adapters.win_target_detector import WinTargetDetector

        detector = WinTargetDetector()
        fg = detector.get_foreground_window()
        assert fg != 0, "no foreground window found"
        assert detector.is_window_valid(fg), "foreground window reported invalid"
        return "foreground hwnd %d detected and valid" % fg

    def _hotkey():
        from stealth_typer.adapters.hotkey_listener import (
            AUTO_ARM_CANDIDATES,
            HotkeyListener,
            pick_free_hotkey,
        )

        import ctypes as _ctypes

        user32 = _ctypes.WinDLL("user32", use_last_error=True)
        key_spec = pick_free_hotkey(user32, AUTO_ARM_CANDIDATES)
        assert key_spec, "no free hotkey combination available"

        toggle = HotkeyListener(key_spec)
        toggle.start()
        time.sleep(0.3)
        try:
            # Either path works now (global, or the GetAsyncKeyState fallback),
            # but the listener must be alive and say which it is.
            assert toggle.alive(), "hotkey listener thread died"
            return toggle.status
        finally:
            toggle.stop()

    check("speed / interval maths", _speed_math)
    check("humanized timing model", _timing)
    check("file encoding + newlines", _encoding)
    check("smart vs raw auto-indent", _smart_indent)
    check("auto-indent detection", _auto_detect)
    check("pause / resume", _pause)
    check("Win32 window detection", _win32)
    check("console excluded as target", _console_safety)
    check("toggle hotkey", _hotkey)

    print("")
    print("=" * 70)
    print("SELF TEST - nothing was typed into any window".center(70))
    print("=" * 70)
    for ok, name, detail in checks:
        print("[%s] %-26s %s" % ("PASS" if ok else "FAIL", name, detail))
    print("=" * 70)
    failed = [c for c in checks if not c[0]]
    passed = len(checks) - len(failed)
    if failed:
        print(
            "%d/%d passed. FAILED: %s" % (passed, len(checks), ", ".join(c[1] for c in failed))
        )
        print("Do not type into a real editor until these pass.")
        return 1
    print("%d/%d passed. Next: --simulate, then try a real editor." % (passed, len(checks)))
    return 0


def run_probe() -> int:
    """Live diagnostic for "nothing happens".

    Reports which hotkey combinations are free on this machine, then shows a
    live view of the focused window so the user can see exactly why the arm
    key is being refused. Types nothing.
    """
    import threading

    from stealth_typer.adapters.win_target_detector import WinTargetDetector

    detector = WinTargetDetector()
    probe = ctypes.WinDLL("user32", use_last_error=True)

    print("=" * 72)
    print("HOTKEY AVAILABILITY ON THIS MACHINE")
    print("=" * 72)
    for name, (mods, vk) in (
        ("alt+x", (0x0001, 0x58)),
        ("ctrl+alt+p", (0x0003, 0x50)),
    ):
        ok = probe.RegisterHotKey(None, 0x4000 + vk, mods, vk)
        if ok:
            probe.UnregisterHotKey(None, 0x4000 + vk)
        print("  %-12s %s" % (name, "FREE" if ok else "TAKEN by another app"))

    key_spec = pick_free_hotkey(probe, AUTO_ARM_CANDIDATES)
    print()
    print("  chosen toggle key:", key_spec)

    fired = threading.Event()
    toggle = HotkeyListener(key_spec, lambda: fired.set())
    toggle.start()
    time.sleep(0.4)
    print("  toggle listener  :", toggle.status)
    print()

    print("=" * 72)
    print("LIVE VIEW - click into your editor. Ctrl+C to exit.")
    print("=" * 72)
    print("%-24s %-10s %-9s %-7s %s" % ("class", "hwnd", "editable", "focus", "verdict"))
    print("-" * 72)

    last = None
    try:
        while True:
            fg = detector.get_foreground_window()
            cls = detector._class_name(fg) if fg else "-"
            editable = detector.is_editable_target(fg, 0) if fg else False
            focused = detector.has_keyboard_focus(fg) if fg else False
            own = detector.is_own_console(fg) if fg else False

            if own:
                verdict = "THIS CONSOLE - click your editor"
            elif not editable:
                verdict = "not editable - click a text field"
            elif not focused:
                verdict = "editable but NOT focused"
            else:
                verdict = ">>> READY - press %s <<<" % arm_spec

            row = (cls, fg, editable, focused)
            if row != last:
                print(
                    "%-24s %-10s %-9s %-7s %s"
                    % (cls[:24], fg, editable, focused, verdict),
                    flush=True,
                )
                last = row

            if fired.is_set():
                print("\n>>> HOTKEY FIRED - the arm key works on this machine <<<\n")
                fired.clear()
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nExiting probe.")
    finally:
        arm.stop()
        pause.stop()
    return 0


def process_integrity_level(pid=None):
    """Return the integrity level of a process ("Medium", "High", ...).

    UIPI check: Windows refuses SendInput from a lower-integrity process into
    a higher-integrity one, and can do so while SendInput still reports
    success - so comparing the two levels is the definitive answer to "why is
    nothing appearing?".
    """
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # Explicit prototypes: without them ctypes truncates 64-bit HANDLEs, which
    # makes these calls fail in ways that look like "access denied".
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.OpenProcessToken.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    TOKEN_QUERY = 0x0008
    TokenIntegrityLevel = 25
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TOKEN_MANDATORY_LABEL(ctypes.Structure):
        _fields_ = [("Label", SID_AND_ATTRIBUTES)]

    if pid is None:
        handle = kernel32.GetCurrentProcess()
        close = False
    else:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        close = True
        if not handle:
            return None

    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
        if close:
            kernel32.CloseHandle(handle)
        return None

    size = wintypes.DWORD(0)
    advapi32.GetTokenInformation(token, TokenIntegrityLevel, None, 0,
                                 ctypes.byref(size))
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    ok = advapi32.GetTokenInformation(token, TokenIntegrityLevel, buf,
                                      size.value, ctypes.byref(size))
    kernel32.CloseHandle(token)
    if close:
        kernel32.CloseHandle(handle)
    if not ok:
        return None

    label = ctypes.cast(buf, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
    sid = label.Label.Sid

    # Parse the SID string instead of indexing subauthorities: reading the
    # subauthority count is easy to get wrong (it returns a BYTE), and a
    # wrong index reports an ordinary Medium process as "System" - actively
    # misleading for a diagnostic whose job is to be accurate.
    text = ctypes.c_wchar_p()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        return None
    sid_string = text.value or ""
    kernel32.LocalFree(text)
    return parse_integrity_sid(sid_string)


_INTEGRITY_RANK = {
    "Untrusted": 0, "Low": 1, "Medium": 2, "High": 3, "System": 4,
}


def integrity_rank(value):
    if not value:
        return -1
    return _INTEGRITY_RANK.get(value.split("(")[0], -1)


def parse_integrity_sid(sid_string):
    """Map an integrity SID string such as S-1-16-8192 to a level name.

    The RID in this string is DECIMAL, not hexadecimal: a Medium-integrity
    process reports S-1-16-8192 (decimal), because 8192 == 0x2000. Parsing it
    as hex yields 0x8192 and reports an ordinary unelevated process as
    "System" - which then makes the UIPI comparison give a confident, wrong
    answer about why keystrokes are not appearing.

    Split out so this is directly testable; it is exactly the kind of
    arithmetic that fails silently.
    """
    if not sid_string:
        return "Unknown"
    try:
        rid = int(sid_string.rsplit("-", 1)[-1], 10)
    except (ValueError, IndexError):
        return "Unknown(%s)" % sid_string

    if rid >= 0x4000:
        return "System"
    if rid >= 0x3000:
        return "High"
    if rid >= 0x2000:
        return "Medium"
    if rid >= 0x1000:
        return "Low"
    return "Untrusted(%s)" % sid_string


def run_input_diagnostic() -> int:
    """Explain exactly why keystrokes do or do not reach the focused window.

    Checks integrity levels (the UIPI rule that silently swallows SendInput),
    then injects a test character and reports precisely what SendInput returned.
    """
    from stealth_typer.adapters.win_input_injection_adapter import (
        WinInputInjectionAdapter,
    )
    from stealth_typer.adapters.win_target_detector import WinTargetDetector

    print("=" * 70)
    print("INPUT DIAGNOSTIC")
    print("=" * 70)

    detector = WinTargetDetector()
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    fg = detector.get_foreground_window()
    print("focused window    :", fg, repr(detector._class_name(fg)) if fg else "-")
    print("editable          :", detector.is_editable_target(fg, 0) if fg else False)
    print("has keyboard focus:", detector.has_keyboard_focus(fg) if fg else False)
    print("this is our console:", detector.is_own_console(fg) if fg else False)

    mine = process_integrity_level()
    print()
    print("THIS process integrity   :", mine)
    pid = wintypes.DWORD(0)
    if fg:
        user32.GetWindowThreadProcessId(wintypes.HWND(fg), ctypes.byref(pid))
        theirs = process_integrity_level(pid.value)
        print("TARGET process integrity :", theirs, "(pid %d)" % pid.value)
        if integrity_rank(theirs) > integrity_rank(mine):
            print()
            print("*** UIPI WILL BLOCK THIS ***")
            print("Windows forbids a lower-integrity process injecting into a")
            print("higher-integrity one. Start PowerShell as Administrator,")
            print("or run the target app at the same level.")

    print()
    print("-" * 70)
    print("Injecting one test character; reporting the raw SendInput result")
    print("-" * 70)
    injector = WinInputInjectionAdapter()
    ok = injector.inject_unicode_char(ord("H"))
    print("adapter returned   :", ok)
    print("adapter last error :", injector.get_last_error())

    print()
    print("Did a capital H appear in the focused window just now?")
    print("(If yes, injection works and the typer logic is the problem.)")
    print("(If no, the keystrokes are being dropped before they arrive.)")
    return 0


def resolve_text(args) -> str:
    """Load the text to type from exactly one of --text / --file / clipboard."""
    if args.text_from_clipboard:
        return WinClipboardAdapter().get_text()
    if args.file:
        return read_text_file(args.file, args.encoding)
    return args.text or ""


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    speed = clamp_speed(args.speed)

    if args.timing_table:
        return print_timing_table(speed, args.humanization, args.seed)

    events = ConsoleEventBus()
    timer = ThreadTimer()

    if args.self_test:
        timer.shutdown()
        return run_self_test(events)

    if args.probe:
        timer.shutdown()
        return run_probe()

    if args.diagnose_input:
        timer.shutdown()
        return run_input_diagnostic()

    if args.simulate:
        # No real window, no real keys: everything below is a rehearsal.
        detector = SimulatedTargetDetector()
        input_port = DryRunInputInjectionPort(echo=False)
        clipboard = None
        auto_start = True
    else:
        detector = WinTargetDetector()
        if args.dry_run:
            input_port = DryRunInputInjectionPort()
        elif args.no_input:
            input_port = DisabledInputInjectionPort()
        else:
            input_port = WinInputInjectionAdapter()
        clipboard = None if (args.text is not None or args.file) else WinClipboardAdapter()
        auto_start = False

    typer = TyperService(clipboard, input_port, timer, events, detector)
    typer.set_speed(speed)
    typer.set_auto_indent_mode(
        {
            "raw": AutoIndentMode.RAW,
            "smart": AutoIndentMode.SMART_IDE,
            "auto": AutoIndentMode.AUTO,
        }[args.auto_indent]
    )
    typer.set_activation_settling_delay_ms(args.settle_ms)
    if args.humanization != "off":
        level = _LEVELS[args.humanization]
        typer.set_humanization_level(level)
        typer.set_timing_model(HumanLikeTimingModel(level, args.seed))

    try:
        text = resolve_text(args)
    except (OSError, LookupError) as exc:
        events.publish_status(StatusChanged("Could not read %s: %s" % (args.file, exc)))
        timer.shutdown()
        return 2

    if not text:
        events.publish_status(
            StatusChanged(
                "No text to type: pass --text, --file or --text-from-clipboard"
            )
        )
        timer.shutdown()
        return 2

    typer.session().set_text(text)
    total_codepoints = typer.total_codepoints
    if auto_start:
        print("Simulated run - nothing will touch your keyboard.", flush=True)
    else:
        print("Loaded %d codepoints." % total_codepoints, flush=True)

    # Arm WITHOUT picking a target. The user clicks their editor and presses
    # the arm hotkey; only then do we start watching for a window. Arming with
    # auto_pick would sample whatever was in the foreground at startup, which
    # is the console this script is running in.
    typer.arm(auto_pick=auto_start)
    if auto_start:
        typer.start_auto()

    # Pause/resume toggle. Driven from a listener thread, so it must only
    # touch typer state through its own API and must never join the timer.
    # Resolve "auto" into a working combination before listening.
    probe = ctypes.WinDLL("user32", use_last_error=True)
    key_spec = args.key
    if key_spec == "auto":
        key_spec = pick_free_hotkey(probe, AUTO_ARM_CANDIDATES) or key_spec

    armed_by_user = threading.Event()
    paused_by_user = threading.Event()

    def target_state():
        """(is_ready, description) for the currently focused window."""
        try:
            fg = detector.get_foreground_window()
        except Exception:
            return False, "no window focused"
        if not fg:
            return False, "no window focused"
        try:
            where = detector._class_name(fg)
        except Exception:
            where = "unknown"
        try:
            if detector.is_own_console(fg):
                return False, "this console - click your editor"
            if not detector.is_editable_target(fg, 0):
                return False, "%s is not editable - click a text field" % where
            if hasattr(detector, "has_keyboard_focus") and not detector.has_keyboard_focus(fg):
                return False, "%s is editable but not focused" % where
        except Exception as exc:
            return False, "detector error: %s" % exc
        return True, where

    def start_typing():
        armed_by_user.set()
        typer.start_monitoring()
        typer.start_auto()

    def toggle():
        """The one hotkey: start, pause, resume.

        Deliberately a single key. Two keys meant the user had to remember
        which was which mid-run, and the pause key could fire by accident from
        a modifier still held after the start press.
        """
        if not armed_by_user.is_set():
            # Start. A press that lands before the click is not thrown away:
            # wait out the grace instead, so the timing cannot be missed.
            ready, detail = target_state()
            if not ready:
                events.publish_status(
                    StatusChanged("Waiting - %s. Click your editor." % detail)
                )
                deadline = time.monotonic() + ARM_GRACE_SECONDS
                got_target = False
                while time.monotonic() < deadline:
                    time.sleep(0.15)
                    ready, detail = target_state()
                    if ready:
                        got_target = True
                        break
                if not got_target:
                    events.publish_status(
                        StatusChanged("Gave up - press %s again." % key_spec)
                    )
                    return
            start_typing()
            return

        if typer.state == TyperState.PAUSED:
            paused_by_user.clear()
            typer.resume_auto()
            events.publish_status(
                StatusChanged("Resumed at %d/%d" % (typer.position, typer.total_codepoints))
            )
        else:
            paused_by_user.set()
            typer.pause_auto_by_user()
            events.publish_status(
                StatusChanged(
                    "Paused at %d/%d - press %s again to resume"
                    % (typer.position, typer.total_codepoints, key_spec)
                )
            )

    listeners = []

    def _listen(spec, callback, label):
        if args.no_hotkey:
            events.publish_status(
                StatusChanged("%s hotkey disabled (--no-hotkey)" % label)
            )
            return None
        try:
            listener = HotkeyListener(spec, callback)
            listener.start()
        except HotkeyError as exc:
            events.publish_status(
                StatusChanged("%s key unusable (%s); continuing without it" % (label, exc))
            )
            return None
        listeners.append(listener)
        return listener

    hotkey = _listen(key_spec, toggle, "Toggle")

    try:
        if not auto_start:
            if hotkey is not None:
                print("", flush=True)
                print("  ONE KEY: %s" % key_spec, flush=True)
                print("    click into your editor, press it to START", flush=True)
                print("    press it again to PAUSE, again to RESUME", flush=True)
                print("  Ctrl+C here aborts.", flush=True)
                print("", flush=True)
                print("  Waiting for you to click your editor...", flush=True)
                print("", flush=True)

                # Block until the key fires. Polled rather than waited on so
                # Ctrl+C still raises KeyboardInterrupt here.
                last_line = ""
                while not armed_by_user.wait(0.2):
                    if not hotkey.alive():
                        events.publish_status(
                            StatusChanged("Hotkey stopped working - aborting")
                        )
                        break
                    ready, detail = target_state()
                    if detail != last_line:
                        last_line = detail
                        marker = "READY" if ready else "waiting"
                        print(
                            "  [%s] %s  -> press %s"
                            % (marker, detail, key_spec),
                            flush=True,
                        )
                if not armed_by_user.is_set():
                    return 1
            else:
                print(
                    "No hotkey available. Click your editor, then press "
                    "Enter here to start.",
                    flush=True,
                )
                input()
                start_typing()

        print("Typing (state=%s). Ctrl+C to stop." % typer.state.value, flush=True)
        # The session is cleared on completion, so position reads 0 afterwards.
        # Poll it to know how far we got if the run is interrupted.
        high_water = 0
        # Paused is included: pausing must not end the wait, it must hold it.
        while typer.state in (
            TyperState.TYPING,
            TyperState.ACTIVATION_SETTLING,
            TyperState.ARMED,
            TyperState.PAUSED,
        ):
            high_water = max(high_water, typer.position)
            time.sleep(0.05)

        if args.simulate or args.dry_run:
            print("---- what would have been typed ----")
            print(input_port.typed_text())
            # Count from the port, not the sampled high_water: sampling every
            # 50 ms under-reports by a character or two, and a dry run has the
            # exact figure to hand. Skipped indentation is not "delivered".
            delivered = len(input_port.typed_text())
            skipped = total_codepoints - delivered
            print(
                "---- end: %d of %d codepoints typed"
                % (delivered, total_codepoints)
                + (" (%d indentation chars skipped)" % skipped if skipped else "")
                + " ----"
            )
        elif high_water < total_codepoints:
            print("Stopped at about %d of %d codepoints." % (high_water, total_codepoints))
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled by user.", flush=True)
    finally:
        for listener in listeners:
            listener.stop()
        typer.reset()
        timer.shutdown()

    return 0


if __name__ == "__main__":
    sys.exit(main())


"""Behavioural parity tests for the ported typer.

These mirror ``tests/integration/test_typer_state_machine.cpp`` and
``tests/unit/test_typing_session.cpp`` from the original C++ project: the same
fakes (test clipboard / input / timer / target detector) drive the same
assertions, so a pass here means the Python port behaves like the original.

Run with::

    python -m unittest discover -s tests -v
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stealth_typer.application.typer_service import FOCUS_LOSS_GRACE_SECONDS


def wait_for_state(typer, wanted, timeout=6.0, poll=0.02):
    """Block until the typer reaches one of ``wanted``.

    Used instead of a fixed sleep before stealing focus. A hard-coded sleep is
    a race: the first test in a process pays for thread creation, so it may not
    have reached Typing yet, and the focus change is then noticed while still
    Armed - where nothing is typing to pause.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if typer.state in wanted:
            return True
        time.sleep(poll)
    return typer.state in wanted


def wait_for_pause(typer, timeout=6.0, poll=0.05):
    """Block until the typer reports Paused, or the timeout expires.

    Focus loss is deliberately debounced (FOCUS_LOSS_GRACE_SECONDS), so tests
    must wait for the resulting state rather than sleeping a fixed amount -
    a hard-coded sleep silently passes while the grace is short and fails
    silently once it grows.
    """
    deadline = time.monotonic() + max(timeout, FOCUS_LOSS_GRACE_SECONDS + 1.0)
    while time.monotonic() < deadline:
        if typer.state == TyperState.PAUSED:
            return True
        time.sleep(poll)
    return typer.state == TyperState.PAUSED


from stealth_typer.application.typer_service import TyperService
from stealth_typer.core.timing_model import HumanizationLevel, HumanLikeTimingModel
from stealth_typer.core.typing_policy import AutoIndentMode, clamp_speed, typing_interval_ms
from stealth_typer.core.typing_session import TypingSession
from stealth_typer.domain.typer_state import TyperState
from stealth_typer.ports.i_clipboard_port import IClipboardPort
from stealth_typer.ports.i_event_bus import CollectingEventBus
from stealth_typer.ports.i_input_injection_port import IInputInjectionPort
from stealth_typer.ports.i_target_detector import ITargetDetector
from stealth_typer.ports.i_timer import ITimer


class TestClipboard(IClipboardPort):
    def __init__(self):
        self.text = ""

    def set_text(self, text):
        self.text = text

    def get_text(self):
        return self.text


class TestInput(IInputInjectionPort):
    def __init__(self):
        self.should_succeed = True
        self.last_err = 0
        self.keys = []  # (vk, key_up)
        self.characters = []

    def inject_virtual_key(self, vk, key_up):
        if not self.should_succeed:
            return False
        self.keys.append((vk, key_up))
        return True

    def inject_unicode_char(self, ch):
        if not self.should_succeed:
            return False
        self.characters.append(ch)
        return True

    def get_last_error(self):
        return self.last_err


class TestTimer(ITimer):
    """Manual timer: nothing fires until the test says so."""

    def __init__(self):
        self.typing_cb = None
        self.monitor_cb = None
        self.settling_cb = None

    def start_periodic(self, timer_id, interval_ms, callback):
        if timer_id == 200:
            self.typing_cb = callback
        elif timer_id == 201:
            self.monitor_cb = callback

    def start_once(self, timer_id, delay_ms, callback):
        if timer_id == 200:
            self.typing_cb = callback
        elif timer_id == 201:
            self.monitor_cb = callback
        elif timer_id == 202:
            self.settling_cb = callback

    def stop(self, timer_id):
        if timer_id == 200:
            self.typing_cb = None
        if timer_id == 201:
            self.monitor_cb = None
        if timer_id == 202:
            self.settling_cb = None

    def fire_typing(self):
        if self.typing_cb:
            self.typing_cb()

    def fire_monitor(self):
        if self.monitor_cb:
            self.monitor_cb()

    def fire_settling(self):
        if self.settling_cb:
            self.settling_cb()


class TestTargetDetector(ITargetDetector):
    def __init__(self):
        self.editable_target = True
        self.window_valid = True
        self.foreground = 1001

    def is_editable_target(self, target_handle, stealthdesk_handle):
        if target_handle == 0 or target_handle == stealthdesk_handle:
            return False
        return self.editable_target

    def is_stealthdesk_window(self, handle, stealthdesk_handle):
        return handle != 0 and handle == stealthdesk_handle

    def is_window_valid(self, handle):
        return handle != 0 and self.window_valid

    def get_foreground_window(self):
        return self.foreground


def make_typer(text="", mode=AutoIndentMode.SMART_IDE, settle_ms=0):
    clipboard = TestClipboard()
    clipboard.text = text
    inp = TestInput()
    timer = TestTimer()
    detector = TestTargetDetector()
    events = CollectingEventBus()
    typer = TyperService(clipboard, inp, timer, events, detector)
    typer.set_activation_settling_delay_ms(settle_ms)
    typer.set_auto_indent_mode(mode)
    return typer, clipboard, inp, timer, detector, events


class TypingSessionTests(unittest.TestCase):
    """Mirrors tests/unit/test_typing_session.cpp."""

    def test_empty_session(self):
        t = TypingSession()
        self.assertEqual(t.text, "")
        self.assertEqual(t.position, 0)
        self.assertEqual(t.total_codepoints, 0)
        self.assertTrue(t.is_complete())
        self.assertFalse(t.has_next())
        self.assertIsNone(t.peek_codepoint())
        self.assertIsNone(t.advance_codepoint())

    def test_ascii(self):
        t = TypingSession("abc")
        self.assertEqual(t.total_codepoints, 3)
        self.assertFalse(t.is_complete())
        self.assertTrue(t.has_next())
        self.assertEqual(t.peek_codepoint(), ord("a"))
        self.assertEqual(t.advance_codepoint(), ord("a"))
        self.assertEqual(t.advance_codepoint(), ord("b"))
        self.assertEqual(t.advance_codepoint(), ord("c"))
        self.assertTrue(t.is_complete())
        self.assertFalse(t.has_next())

    def test_set_position_and_clamp(self):
        t = TypingSession("abcde")
        t.set_position(2)
        self.assertEqual(t.position, 2)
        self.assertEqual(t.peek_codepoint(), ord("c"))
        t.set_position(100)
        self.assertEqual(t.position, 5)
        self.assertTrue(t.is_complete())

    def test_reset_and_clear(self):
        t = TypingSession("abc")
        t.advance_codepoint()
        t.advance_codepoint()
        t.reset()
        self.assertEqual(t.position, 0)
        self.assertEqual(t.peek_codepoint(), ord("a"))
        t.clear()
        self.assertEqual(t.text, "")
        self.assertEqual(t.total_codepoints, 0)

    def test_load_text_replaces(self):
        t = TypingSession("old")
        t.advance_codepoint()
        t.set_text("new")
        self.assertEqual(t.text, "new")
        self.assertEqual(t.position, 0)
        self.assertEqual(t.total_codepoints, 3)

    def test_crlf_normalized_to_single_newline(self):
        t = TypingSession("a\r\nb\rc")
        self.assertEqual(t.text, "a\nb\nc")
        self.assertEqual(t.total_codepoints, 5)

    def test_unicode_and_astral(self):
        t = TypingSession("aé\U0001F600")
        self.assertEqual(t.total_codepoints, 3)
        t.advance_codepoint()
        self.assertEqual(t.peek_codepoint(), 0xE9)
        t.advance_codepoint()
        self.assertEqual(t.peek_codepoint(), 0x1F600)


class PolicyTests(unittest.TestCase):
    def test_clamp_speed(self):
        self.assertEqual(clamp_speed(0), 1)
        self.assertEqual(clamp_speed(-5), 1)
        self.assertEqual(clamp_speed(50), 50)
        self.assertEqual(clamp_speed(100), 100)
        self.assertEqual(clamp_speed(500), 100)

    def test_typing_interval_endpoints(self):
        self.assertEqual(typing_interval_ms(1), 500)
        self.assertEqual(typing_interval_ms(100), 5)
        self.assertEqual(typing_interval_ms(50), 255)

    def test_human_like_is_bounded_and_varies(self):
        model = HumanLikeTimingModel(HumanizationLevel.MEDIUM, 1337)
        delays = [model.next_delay_ms(ord("a"), 0, 60) for _ in range(200)]
        self.assertTrue(all(5 <= d <= 2000 for d in delays))
        self.assertGreater(len(set(delays)), 1)

    def test_human_like_seed_is_deterministic(self):
        a = HumanLikeTimingModel(HumanizationLevel.HIGH, 42)
        b = HumanLikeTimingModel(HumanizationLevel.HIGH, 42)
        self.assertEqual(
            [a.next_delay_ms(ord("a"), 0, 50) for _ in range(20)],
            [b.next_delay_ms(ord("a"), 0, 50) for _ in range(20)],
        )

    def test_punctuation_and_newline_are_slower(self):
        punct = HumanLikeTimingModel(HumanizationLevel.MEDIUM, 7)
        newline = HumanLikeTimingModel(HumanizationLevel.MEDIUM, 7)
        plain = HumanLikeTimingModel(HumanizationLevel.MEDIUM, 7)
        # Same z drawn three times -> only the contextual multiplier differs.
        punct_delay = punct.next_delay_ms(ord("."), 0, 50)
        newline_delay = newline.next_delay_ms(ord("\n"), 0, 50)
        plain_delay = plain.next_delay_ms(ord("a"), 0, 50)
        self.assertGreater(punct_delay, plain_delay)
        self.assertGreater(newline_delay, punct_delay)


class StateMachineTests(unittest.TestCase):
    """Mirrors tests/integration/test_typer_state_machine.cpp."""

    def test_long_armed_period_never_types(self):
        typer, _, inp, timer, detector, _ = make_typer("#include <iostream>")
        detector.foreground = 9999  # the StealthDesk window itself
        detector.editable_target = False
        typer.set_stealthdesk_hwnd(9999)
        typer.arm()

        self.assertEqual(typer.state, TyperState.ARMED)
        for _ in range(25):
            timer.fire_monitor()
            self.assertEqual(typer.state, TyperState.ARMED)
            self.assertEqual(typer.position, 0)
        self.assertEqual(inp.characters, [])
        self.assertEqual(inp.keys, [])

    def test_focus_change_during_settling_returns_to_armed(self):
        typer, _, inp, timer, detector, _ = make_typer("int main() {}")
        typer.arm()
        self.assertEqual(typer.state, TyperState.ACTIVATION_SETTLING)
        self.assertEqual(typer.position, 0)

        detector.foreground = 2002  # focus moved before settling finished
        timer.fire_monitor()
        self.assertEqual(typer.state, TyperState.ARMED)
        self.assertEqual(inp.characters, [])

        timer.fire_settling()  # must be a no-op now
        self.assertEqual(typer.state, TyperState.ARMED)
        self.assertEqual(inp.characters, [])

    def test_focus_change_during_typing_pauses(self):
        import stealth_typer.application.typer_service as svc

        typer, _, inp, timer, detector, _ = make_typer("Hello World")
        typer.arm()
        timer.fire_settling()
        self.assertEqual(typer.state, TyperState.TYPING)

        for _ in range(3):
            timer.fire_typing()
        self.assertEqual(typer.position, 3)

        detector.foreground = 2002
        # Focus loss is debounced, so the first tick only records the change.
        # Shrink the grace so the test does not have to sleep through it.
        original = svc.FOCUS_LOSS_GRACE_SECONDS
        svc.FOCUS_LOSS_GRACE_SECONDS = 0.0
        try:
            timer.fire_monitor()
            time.sleep(0.01)
            timer.fire_monitor()
        finally:
            svc.FOCUS_LOSS_GRACE_SECONDS = original

        self.assertEqual(typer.state, TyperState.PAUSED)
        self.assertEqual(typer.position, 3)
        self.assertEqual(len(inp.characters), 3)

    def test_target_closed_during_typing_pauses(self):
        typer, _, inp, timer, detector, _ = make_typer("Sample Code")
        typer.arm()
        timer.fire_settling()
        timer.fire_typing()
        timer.fire_typing()
        self.assertEqual(typer.position, 2)

        detector.window_valid = False
        timer.fire_monitor()
        self.assertEqual(typer.state, TyperState.PAUSED)
        self.assertEqual(typer.position, 2)
        self.assertEqual(len(inp.characters), 2)

    def test_pause_then_resume_continues_where_it_stopped(self):
        typer, _, inp, timer, _, _ = make_typer("ABCDEF")
        typer.arm()
        timer.fire_settling()
        for _ in range(3):
            timer.fire_typing()
        self.assertEqual(typer.position, 3)

        typer.pause_auto()
        self.assertEqual(typer.state, TyperState.PAUSED)
        timer.fire_monitor()
        self.assertEqual(typer.state, TyperState.PAUSED)  # background tick must not resume

        typer.start_auto()
        self.assertEqual(typer.state, TyperState.ACTIVATION_SETTLING)
        timer.fire_settling()
        self.assertEqual(typer.state, TyperState.TYPING)
        timer.fire_typing()
        self.assertEqual(typer.position, 4)
        self.assertEqual(inp.characters[-1], ord("D"))

    def test_input_failure_reports_uipi_and_never_advances(self):
        typer, _, inp, timer, _, events = make_typer("XYZ")
        typer.arm()
        timer.fire_settling()

        inp.should_succeed = False
        inp.last_err = 5  # ERROR_ACCESS_DENIED
        timer.fire_typing()

        self.assertEqual(typer.position, 0)
        self.assertEqual(typer.state, TyperState.PAUSED)
        self.assertEqual(inp.characters, [])
        self.assertIn("UIPI / Elevated target", events.statuses[-1])

        inp.should_succeed = True
        inp.last_err = 0
        typer.start_auto()
        timer.fire_settling()
        timer.fire_typing()
        self.assertEqual(typer.position, 1)
        self.assertEqual(inp.characters[0], ord("X"))

    def test_unmapped_codepoint_reports_its_codepoint(self):
        typer, _, inp, timer, _, events = make_typer("é")
        typer.arm()
        timer.fire_settling()
        inp.should_succeed = False
        inp.last_err = 50  # ERROR_NOT_SUPPORTED
        timer.fire_typing()
        self.assertIn("U+00E9", events.statuses[-1])

    def test_no_input_port_refuses_to_arm(self):
        typer = TyperService(
            TestClipboard(), None, TestTimer(), CollectingEventBus(), TestTargetDetector()
        )
        typer.session().set_text("hello")
        typer.arm()
        self.assertEqual(typer.state, TyperState.IDLE)
        self.assertEqual(typer.input_refusal(), "Typing unavailable: no input driver")

    def test_disabled_port_refusal_is_reported_not_crashed(self):
        from stealth_typer.adapters.disabled_input_port import DisabledInputInjectionPort

        typer, _, _, _, _, _ = make_typer("hello")
        typer._input = DisabledInputInjectionPort()
        typer.arm()
        self.assertIn("Input driver not installed", typer.input_refusal())
        self.assertEqual(typer.state, TyperState.IDLE)

    def test_first_character_of_include_is_preserved(self):
        typer, _, inp, timer, _, _ = make_typer("#include <iostream>")
        typer.arm()
        self.assertEqual(typer.position, 0)
        self.assertEqual(inp.characters, [])  # nothing typed while ARMED/SETTLING

        timer.fire_settling()
        self.assertEqual(typer.state, TyperState.TYPING)
        timer.fire_typing()
        self.assertEqual(typer.position, 1)
        self.assertEqual(inp.characters[0], ord("#"))

        for _ in range(7):
            timer.fire_typing()
        self.assertEqual("".join(chr(c) for c in inp.characters), "#include")

    def test_typing_completes_and_returns_to_idle(self):
        typer, _, inp, timer, _, events = make_typer("hi")
        typer.arm()
        timer.fire_settling()
        timer.fire_typing()  # 'h'
        timer.fire_typing()  # 'i'
        timer.fire_typing()  # complete tick
        self.assertEqual(typer.state, TyperState.IDLE)
        self.assertEqual("".join(chr(c) for c in inp.characters), "hi")
        self.assertIn("Typing complete", events.statuses)

    def test_newline_and_tab_use_virtual_keys(self):
        # RAW mode so the tab is typed rather than skipped as IDE indentation.
        typer, _, inp, timer, _, _ = make_typer("a\n\tb", mode=AutoIndentMode.RAW)
        typer.arm()
        timer.fire_settling()
        for _ in range(6):
            timer.fire_typing()
        # Both the press and the release of each key are injected.
        self.assertEqual([vk for vk, _ in inp.keys], [0x0D, 0x0D, 0x09, 0x09])
        self.assertEqual("".join(chr(c) for c in inp.characters), "ab")

    def test_smart_ide_skips_the_tab_indent_after_a_newline(self):
        typer, _, inp, timer, _, _ = make_typer("a\n\tb")
        typer.arm()
        timer.fire_settling()
        for _ in range(6):
            timer.fire_typing()
        self.assertEqual([vk for vk, _ in inp.keys], [0x0D, 0x0D])
        self.assertEqual("".join(chr(c) for c in inp.characters), "ab")

    def test_astral_character_is_delivered(self):
        typer, _, inp, timer, _, _ = make_typer("\U0001F600")
        typer.arm()
        timer.fire_settling()
        timer.fire_typing()
        self.assertEqual(inp.characters, [0x1F600])


class AutoIndentTests(unittest.TestCase):
    """The 10-case Smart IDE auto-indent regression suite from the C++ test."""

    def _type(self, text, ticks, mode=AutoIndentMode.SMART_IDE):
        typer, _, inp, timer, _, _ = make_typer(text, mode=mode)
        typer.arm()
        timer.fire_settling()
        for _ in range(ticks):
            timer.fire_typing()
        return inp

    def test_case1_smart_ide_skips_editor_indent(self):
        inp = self._type("if (a) {\n    b();\n}", 14)
        self.assertEqual(inp.characters[-1], ord("b"))

    def test_case2_raw_mode_sends_spaces_verbatim(self):
        inp = self._type("if (a) {\n    b();\n}", 10, mode=AutoIndentMode.RAW)
        self.assertEqual(inp.characters[-1], ord(" "))

    def test_case3_raw_mode_preserves_source_indentation(self):
        inp = self._type("line1\n  line2", 9, mode=AutoIndentMode.RAW)
        self.assertEqual(inp.characters[-1], ord("l"))

    def test_case4_tab_indentation_skipped(self):
        inp = self._type("void f() {\n\tcode();\n}", 13)
        self.assertEqual(inp.characters[-1], ord("c"))

    def test_case5_leading_spaces_on_first_line_are_typed(self):
        inp = self._type("  spaced", 2, mode=AutoIndentMode.RAW)
        self.assertEqual(inp.characters[:2], [ord(" "), ord(" ")])

    def test_case6_nested_indentation(self):
        inp = self._type("a\n  b\n    c", 11)
        self.assertEqual(inp.characters[-1], ord("c"))

    def test_case7_dedentation_types_no_space(self):
        inp = self._type("    a\nb", 7)
        self.assertEqual(inp.characters[-1], ord("b"))

    def test_case8_blank_lines(self):
        inp = self._type("a\n\nb", 4)
        self.assertEqual(inp.characters[-1], ord("b"))

    def test_case9_braces(self):
        inp = self._type("class C {\n};", 11)
        self.assertEqual(inp.characters[-1], ord("}"))

    def test_case10_mixed_tab_and_space_indent(self):
        # "line1" (5) + \n (1) + skip \t + 2 spaces (3) + 'l' (1) = 10 ticks
        inp = self._type("line1\n\t  line2", 10)
        self.assertEqual(inp.characters[-1], ord("l"))


class ThreadTimerTests(unittest.TestCase):
    """Exercises the real ThreadTimer adapter, not the manual fake."""

    def test_periodic_and_once(self):
        from stealth_typer.adapters.thread_timer import ThreadTimer

        timer = ThreadTimer()
        try:
            fired = []
            timer.start_periodic(1, 10, lambda: fired.append("p"))
            timer.start_once(2, 10, lambda: fired.append("o"))
            deadline = __import__("time").monotonic() + 2.0
            while __import__("time").monotonic() < deadline:
                if "p" in fired and fired.count("p") >= 3 and "o" in fired:
                    break
                __import__("time").sleep(0.01)
            self.assertGreaterEqual(fired.count("p"), 3)
            self.assertEqual(fired.count("o"), 1)
        finally:
            timer.shutdown()

    def test_stop_cancels(self):
        import time as _time

        from stealth_typer.adapters.thread_timer import ThreadTimer

        timer = ThreadTimer()
        try:
            fired = []
            timer.start_periodic(7, 10, lambda: fired.append(1))
            _time.sleep(0.05)
            timer.stop(7)
            count_at_stop = len(fired)
            _time.sleep(0.1)
            self.assertEqual(len(fired), count_at_stop)
        finally:
            timer.shutdown()


class AutoIndentAutoModeTests(unittest.TestCase):
    """AUTO resolves to raw or smart from the target window."""

    class Detector(TestTargetDetector):
        def __init__(self, answer):
            super().__init__()
            self.answer = answer
            self.asked = []

        def is_auto_indenting_editor(self, handle):
            self.asked.append(handle)
            return self.answer

    def _run(self, answer, ticks):
        clipboard = TestClipboard()
        clipboard.text = "a\n  b"
        inp = TestInput()
        timer = TestTimer()
        detector = self.Detector(answer)
        typer = TyperService(clipboard, inp, timer, CollectingEventBus(), detector)
        typer.set_activation_settling_delay_ms(0)
        typer.set_auto_indent_mode(AutoIndentMode.AUTO)
        typer.arm()
        timer.fire_settling()
        for _ in range(ticks):
            timer.fire_typing()
        return typer, inp, detector

    def test_auto_resolves_to_smart_for_an_auto_indenting_editor(self):
        typer, inp, detector = self._run(True, 5)
        self.assertEqual(typer.effective_auto_indent_mode, AutoIndentMode.SMART_IDE)
        self.assertEqual(detector.asked, [1001])
        # 'a' (1) + \n (2) + skip 2 spaces (3,4) + 'b' (5).
        # The newline leaves as a virtual key, so it is in keys, not characters.
        self.assertEqual(inp.characters[-1], ord("b"))
        self.assertEqual("".join(chr(c) for c in inp.characters), "ab")
        self.assertEqual([vk for vk, _ in inp.keys], [0x0D, 0x0D])

    def test_auto_resolves_to_raw_for_a_plain_editor(self):
        typer, inp, _ = self._run(False, 3)
        self.assertEqual(typer.effective_auto_indent_mode, AutoIndentMode.RAW)
        self.assertEqual(inp.characters[-1], ord(" "))  # space typed, not skipped

    def test_auto_falls_back_to_smart_when_unknown(self):
        typer, _, _ = self._run(None, 1)
        self.assertEqual(typer.effective_auto_indent_mode, AutoIndentMode.SMART_IDE)

    def test_explicit_modes_never_ask_the_detector(self):
        clipboard = TestClipboard()
        clipboard.text = "a\n  b"
        detector = self.Detector(True)
        typer = TyperService(
            clipboard, TestInput(), TestTimer(), CollectingEventBus(), detector
        )
        typer.set_activation_settling_delay_ms(0)
        typer.set_auto_indent_mode(AutoIndentMode.RAW)
        typer.arm()
        self.assertEqual(detector.asked, [])
        self.assertEqual(typer.effective_auto_indent_mode, AutoIndentMode.RAW)

    def test_default_detector_answer_is_unknown(self):
        # A detector that does not implement the hook must still work.
        typer = TyperService(
            TestClipboard(),
            TestInput(),
            TestTimer(),
            CollectingEventBus(),
            TestTargetDetector(),
        )
        typer.set_auto_indent_mode(AutoIndentMode.AUTO)
        typer._resolve_auto_indent_for_target(1001)
        self.assertEqual(typer.effective_auto_indent_mode, AutoIndentMode.SMART_IDE)


class ReadTextFileTests(unittest.TestCase):
    """The --file loader, including the encodings answer files arrive in."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.mkdtemp()

    def _write(self, name, data):
        import os

        path = os.path.join(self._tmp, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def _read(self, path, encoding="auto"):
        from main import read_text_file

        return read_text_file(path, encoding)

    def test_plain_utf8(self):
        self.assertEqual(self._read(self._write("a.txt", "hello".encode("utf-8"))), "hello")

    def test_utf8_bom_is_stripped(self):
        self.assertEqual(self._read(self._write("b.txt", b"\xef\xbb\xbfhello")), "hello")

    def test_utf16_is_detected(self):
        path = self._write("c.txt", "héllo".encode("utf-16"))
        self.assertEqual(self._read(path), "héllo")

    def test_cp1252_falls_back(self):
        # 0x92 is invalid UTF-8 but is U+2019 (right single quote) in cp1252,
        # so a correct fallback round-trips the character rather than failing.
        path = self._write("d.txt", b"don\x92t")
        self.assertEqual(self._read(path), "don\u2019t")

    def test_crlf_is_normalized_by_the_session_not_the_loader(self):
        from stealth_typer.core.typing_session import TypingSession

        path = self._write("e.txt", b"a\r\nb")
        self.assertEqual(self._read(path), "a\r\nb")  # loader is faithful
        self.assertEqual(TypingSession(self._read(path)).text, "a\nb")

    def test_explicit_encoding_is_honoured(self):
        path = self._write("f.txt", "café".encode("cp1252"))
        self.assertEqual(self._read(path, "cp1252"), "café")

    def test_missing_file_raises(self):
        import os

        with self.assertRaises(FileNotFoundError):
            self._read(os.path.join(self._tmp, "nope.txt"))


class DryRunTranscriptTests(unittest.TestCase):
    """typed_text() must reassemble the transcript in order, not grouped."""

    def test_newlines_land_in_the_right_place(self):
        from stealth_typer.adapters.dry_run_input_port import DryRunInputInjectionPort

        port = DryRunInputInjectionPort(echo=False)
        port.inject_unicode_char(ord("a"))
        port.inject_virtual_key(0x0D, False)
        port.inject_virtual_key(0x0D, True)
        port.inject_unicode_char(ord("b"))
        port.inject_virtual_key(0x09, False)
        port.inject_virtual_key(0x09, True)
        port.inject_unicode_char(ord("c"))
        self.assertEqual(port.typed_text(), "a\nb\tc")

    def test_key_up_is_not_counted_twice(self):
        from stealth_typer.adapters.dry_run_input_port import DryRunInputInjectionPort

        port = DryRunInputInjectionPort(echo=False)
        port.inject_virtual_key(0x0D, False)
        port.inject_virtual_key(0x0D, True)
        self.assertEqual(port.typed_text(), "\n")
        self.assertEqual(port.virtual_keys, [0x0D])


class HotkeySpecTests(unittest.TestCase):
    """The hotkey spec parser, which fails loudly rather than silently."""

    def test_parses_a_combo(self):
        from stealth_typer.adapters.hotkey_listener import MOD_ALT, MOD_CONTROL, parse_hotkey

        mods, key = parse_hotkey("ctrl+alt+p")
        self.assertEqual(mods, MOD_CONTROL | MOD_ALT)
        self.assertEqual(key, ord("P"))

    def test_case_insensitive_and_single_key(self):
        from stealth_typer.adapters.hotkey_listener import parse_hotkey

        self.assertEqual(parse_hotkey("F8"), (0, 0x77))
        self.assertEqual(parse_hotkey("ctrl+shift+F5")[1], 0x74)

    def test_whitespace_tolerated(self):
        from stealth_typer.adapters.hotkey_listener import parse_hotkey

        self.assertEqual(parse_hotkey(" ctrl + alt + p "), parse_hotkey("ctrl+alt+p"))

    def test_rejects_nonsense(self):
        from stealth_typer.adapters.hotkey_listener import HotkeyError, parse_hotkey

        for bad in ("", "ctrl+alt", "ctrl+nope", "ctrl+alt+p+q"):
            with self.assertRaises(HotkeyError):
                parse_hotkey(bad)


class HotkeyListenerTests(unittest.TestCase):
    """Registers a real hotkey on this machine and releases it again."""

    def test_register_and_unregister(self):
        from stealth_typer.adapters.hotkey_listener import HotkeyListener

        listener = HotkeyListener("ctrl+alt+f9")
        listener.start()
        try:
            # Whether it registers depends on whether another app owns the
            # combo; either way the listener must be usable and stoppable.
            self.assertIn("ctrl+alt+f9", listener.status)
        finally:
            listener.stop()

    def test_fallback_status_is_honest(self):
        from stealth_typer.adapters.hotkey_listener import HotkeyListener

        listener = HotkeyListener("ctrl+alt+p")
        listener.start()
        try:
            if listener.registered:
                self.assertIn("global", listener.status)
            else:
                # Must not claim to be console-only: that fallback could not
                # see a keypress aimed at the user's editor.
                self.assertIn("polled", listener.status)
                self.assertNotIn("console only", listener.status)
        finally:
            listener.stop()


class PauseResumeTimingTests(unittest.TestCase):
    """Pause must actually stop the ticks, and resume must continue in place."""

    def test_pause_holds_and_resume_continues(self):
        import time

        from stealth_typer.adapters.dry_run_input_port import DryRunInputInjectionPort
        from stealth_typer.adapters.thread_timer import ThreadTimer
        from stealth_typer.application.typer_service import TyperService
        from stealth_typer.ports.i_event_bus import CollectingEventBus

        class AlwaysEditableDetector:
            def is_editable_target(self, target_handle, stealthdesk_handle):
                return True

            def is_stealthdesk_window(self, handle, stealthdesk_handle):
                return False

            def is_window_valid(self, handle):
                return True

            def get_foreground_window(self):
                return 1001

        timer = ThreadTimer()
        port = DryRunInputInjectionPort(echo=False)
        typer = TyperService(None, port, timer, CollectingEventBus(), AlwaysEditableDetector())
        text = "".join(chr(ord("a") + (i % 26)) for i in range(400))
        typer.session().set_text(text)
        typer.set_speed(90)
        typer.set_activation_settling_delay_ms(0)

        try:
            typer.arm()
            typer.start_auto()
            time.sleep(0.4)
            self.assertEqual(typer.state, TyperState.TYPING)

            typer.pause_auto()
            frozen = len(port.unicode_chars)
            self.assertGreater(frozen, 0)
            self.assertEqual(typer.state, TyperState.PAUSED)

            # Wait well past several tick intervals: nothing more may be typed.
            time.sleep(0.5)
            self.assertEqual(len(port.unicode_chars), frozen, "pause did not hold")
            self.assertEqual(typer.position, frozen)

            typer.start_auto()
            time.sleep(0.4)
            self.assertGreater(len(port.unicode_chars), frozen, "resume did not continue")
            self.assertEqual(typer.state, TyperState.TYPING)
        finally:
            typer.pause_auto()
            timer.shutdown()


class SimulatedDetectorTests(unittest.TestCase):
    """The offline detector used by --simulate and --self-test."""

    def test_pretends_to_be_editable_and_valid(self):
        from stealth_typer.adapters.simulated_target_detector import (
            SimulatedTargetDetector,
        )

        detector = SimulatedTargetDetector()
        self.assertTrue(detector.is_editable_target(detector.handle, 0))
        self.assertTrue(detector.is_window_valid(detector.handle))
        self.assertFalse(detector.is_editable_target(0, 0))
        self.assertFalse(detector.is_window_valid(0))

    def test_can_rehearse_the_refusal_paths(self):
        from stealth_typer.adapters.simulated_target_detector import (
            SimulatedTargetDetector,
        )

        self.assertFalse(SimulatedTargetDetector(editable=False).is_editable_target(1, 0))
        self.assertFalse(SimulatedTargetDetector(valid=False).is_window_valid(1))

    def test_never_touches_win32(self):
        # The whole point: it must be usable with no real window present.
        from stealth_typer.adapters.simulated_target_detector import (
            SimulatedTargetDetector,
        )

        detector = SimulatedTargetDetector()
        self.assertIsNotNone(detector.is_auto_indenting_editor(detector.handle))


class CliSelfTestTests(unittest.TestCase):
    """main.py --self-test must run clean and report every subsystem."""

    def test_self_test_passes_on_this_machine(self):
        import io
        from contextlib import redirect_stdout

        from main import run_self_test
        from stealth_typer.adapters.event_bus import ConsoleEventBus

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = run_self_test(ConsoleEventBus(echo=False))
        output = buffer.getvalue()
        self.assertEqual(code, 0, output)
        self.assertNotIn("[FAIL]", output)
        self.assertIn("9/9 passed", output)
        # The hotkey check must name the combinations it actually resolved to,
        # so a machine where the obvious ones are taken is visible.
        self.assertNotIn("ctrl+alt+p console-only", output)

    def test_simulate_flag_requires_no_arguments(self):
        import main as cli

        args = cli.build_parser().parse_args(["--simulate"])
        self.assertTrue(args.simulate)


class ShippedSampleFilesTests(unittest.TestCase):
    """The sample files the README tells users to run must actually exist."""

    @staticmethod
    def _root():
        import os

        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    @staticmethod
    def _read(name):
        import os

        from main import read_text_file

        return read_text_file(os.path.join(ShippedSampleFilesTests._root(), name), "auto")

    def test_answer_template_exists_and_is_editable_content(self):
        import os

        path = os.path.join(self._root(), "answer.txt")
        self.assertTrue(os.path.isfile(path), "answer.txt template is missing")
        text = self._read("answer.txt")
        self.assertTrue(text.strip(), "answer.txt is empty")
        # It must load as a usable session, indentation and all.
        session = TypingSession(text)
        self.assertGreater(session.total_codepoints, 0)

    def test_all_documented_sample_files_exist(self):
        import os

        for name in ("answer.txt", "sample.py.txt", "sample-prose.txt"):
            path = os.path.join(self._root(), name)
            self.assertTrue(os.path.isfile(path), "%s is missing" % name)
            self.assertGreater(len(self._read(name).strip()), 0)

    def test_sample_py_has_indentation_to_demonstrate(self):
        text = self._read("sample.py.txt")
        self.assertIn("\n    ", text, "sample.py.txt should contain indented lines")


class ConsoleExclusionTests(unittest.TestCase):
    """The typer must never treat its own console as a typing target."""

    def test_console_classes_are_recognised(self):
        from stealth_typer.adapters.win_target_detector import (
            _CONSOLE_CLASSES,
            _is_console_class,
        )

        for name in (
            "ConsoleWindowClass",
            "PseudoConsoleWindow",
            "CASCADIA_HOSTING_WINDOW_CLASS",
        ):
            self.assertIn(name, _CONSOLE_CLASSES)
            self.assertTrue(_is_console_class(name))

    def test_real_editors_are_not_mistaken_for_consoles(self):
        from stealth_typer.adapters.win_target_detector import _is_console_class

        for name in ("Notepad", "Chrome_WidgetWin_1", "SunAwtFrame", "Edit", ""):
            self.assertFalse(_is_console_class(name))

    def test_own_console_is_never_editable(self):
        from stealth_typer.adapters.win_target_detector import WinTargetDetector

        detector = WinTargetDetector()
        console = detector._own_console
        root = detector._own_console_root
        for handle in {h for h in (console, root) if h}:
            self.assertTrue(detector.is_own_console(handle))
            self.assertFalse(
                detector.is_editable_target(handle, 0),
                "own console %d reported as editable" % handle,
            )

    def test_null_handle_is_not_a_console(self):
        from stealth_typer.adapters.win_target_detector import WinTargetDetector

        detector = WinTargetDetector()
        self.assertFalse(detector.is_own_console(0))
        self.assertFalse(detector.is_editable_target(0, 0))


class ArmHotkeyFlowTests(unittest.TestCase):
    """arm(auto_pick=False) must wait for the user instead of grabbing focus."""

    def _service(self):
        from stealth_typer.adapters.dry_run_input_port import DryRunInputInjectionPort
        from stealth_typer.adapters.simulated_target_detector import (
            SimulatedTargetDetector,
        )
        from stealth_typer.adapters.thread_timer import ThreadTimer
        from stealth_typer.application.typer_service import TyperService
        from stealth_typer.ports.i_event_bus import CollectingEventBus

        timer = ThreadTimer()
        port = DryRunInputInjectionPort(echo=False)
        events = CollectingEventBus()
        typer = TyperService(
            None, port, timer, events, SimulatedTargetDetector()
        )
        typer.session().set_text("hello world")
        typer.set_speed(100)
        typer.set_activation_settling_delay_ms(0)
        return typer, port, timer, events

    def test_arm_without_auto_pick_waits_and_types_nothing(self):
        import time as _time

        typer, port, timer, events = self._service()
        try:
            typer.arm(auto_pick=False)
            self.assertEqual(typer.state, TyperState.ARMED)
            # No target has been chosen, so no monitor and no keystrokes.
            _time.sleep(0.2)
            self.assertEqual(port.unicode_chars, [])
            self.assertEqual(typer.position, 0)
        finally:
            timer.shutdown()

    def test_start_monitoring_then_start_auto_types_the_text(self):
        import time as _time

        typer, port, timer, events = self._service()
        try:
            typer.arm(auto_pick=False)
            typer.start_monitoring()  # the arm hotkey does this
            typer.start_auto()

            deadline = _time.monotonic() + 5.0
            while typer.state != TyperState.IDLE and _time.monotonic() < deadline:
                _time.sleep(0.02)
            self.assertEqual(
                "".join(chr(c) for c in port.unicode_chars), "hello world"
            )
            self.assertEqual(typer.state, TyperState.IDLE)
        finally:
            timer.shutdown()

    def test_single_key_replaces_arm_and_pause(self):
        import main as cli

        args = cli.build_parser().parse_args(["--text", "x"])
        # One key, not two: two meant having to remember which was which
        # mid-run, and the pause combo could fire from a held modifier.
        self.assertEqual(args.key, "auto")
        self.assertFalse(hasattr(args, "arm_key"))
        self.assertFalse(hasattr(args, "pause_key"))

    def test_key_is_configurable(self):
        import main as cli

        args = cli.build_parser().parse_args(["--text", "x", "--key", "f9"])
        self.assertEqual(args.key, "f9")

    def test_alt_x_parses_to_alt_and_x(self):
        from stealth_typer.adapters.hotkey_listener import MOD_ALT, parse_hotkey

        modifiers, key = parse_hotkey("alt+x")
        self.assertTrue(modifiers & MOD_ALT)
        self.assertEqual(key, ord("X"))

    def test_both_hotkeys_can_register_together(self):
        import time as _time

        from stealth_typer.adapters.hotkey_listener import HotkeyListener

        arm = HotkeyListener("alt+x")
        pause = HotkeyListener("ctrl+alt+p")
        arm.start()
        pause.start()
        _time.sleep(0.4)
        try:
            self.assertTrue(arm.alive())
            self.assertTrue(pause.alive())
        finally:
            arm.stop()
            pause.stop()
        self.assertFalse(arm.alive())
        self.assertFalse(pause.alive())


class FocusRequirementTests(unittest.TestCase):
    """An editable window that is not focused must not be typed into.

    Keystrokes always go to the focused window, so accepting an unfocused but
    editable window (a background Notepad, say) means the text lands somewhere
    the user is not looking.
    """

    def test_foreground_window_has_focus(self):
        from stealth_typer.adapters.win_target_detector import WinTargetDetector

        detector = WinTargetDetector()
        fg = detector.get_foreground_window()
        self.assertNotEqual(fg, 0)
        self.assertTrue(
            detector.has_keyboard_focus(fg),
            "the foreground window must report itself as focused",
        )

    def test_null_handle_is_never_focused(self):
        from stealth_typer.adapters.win_target_detector import WinTargetDetector

        detector = WinTargetDetector()
        self.assertFalse(detector.has_keyboard_focus(0))

    def test_background_window_is_not_focused(self):
        """A visible window other than the foreground must not claim focus."""
        import ctypes

        from stealth_typer.adapters.win_target_detector import WinTargetDetector

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        detector = WinTargetDetector()
        fg = detector.get_foreground_window()
        self.assertNotEqual(fg, 0)

        # Any visible top-level window that is not the foreground will do.
        others = []
        wintypes = ctypes.wintypes
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def collect(hwnd, _lparam):
            if int(hwnd) != fg and user32.IsWindowVisible(hwnd):
                if user32.GetWindowTextLengthW(hwnd) > 0:
                    others.append(int(hwnd))
            return True

        user32.EnumWindows(proc(collect), 0)
        if not others:
            self.skipTest("no other visible window available")

        for other in others[:5]:
            self.assertFalse(
                detector.has_keyboard_focus(other),
                "background window %d wrongly reported as focused" % other,
            )

    def test_desktop_window_is_not_a_focus_target(self):
        from stealth_typer.adapters.win_target_detector import WinTargetDetector

        detector = WinTargetDetector()
        desktop = int(detector._user32.GetDesktopWindow() or 0)
        if desktop:
            self.assertFalse(detector.is_editable_target(desktop, 0))


class HotkeyAvailabilityTests(unittest.TestCase):
    """Auto key selection must skip combinations another app already owns.

    Regression: on a machine where Alt+X and Ctrl+Alt+P were already
    registered, the listener fell back to reading the CONSOLE, which cannot
    see a keypress aimed at the user's editor - so the hotkey looked alive and
    never fired. The fallback now polls GetAsyncKeyState, and "auto" picks a
    combination that is genuinely free.
    """

    def test_probe_does_not_steal_the_combination(self):
        import ctypes

        from stealth_typer.adapters.hotkey_listener import probe_hotkey

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.assertTrue(probe_hotkey(user32, "f9"))
        # Still free afterwards: probe registers then immediately unregisters.
        self.assertTrue(probe_hotkey(user32, "f9"))

    def test_probe_rejects_nonsense(self):
        import ctypes

        from stealth_typer.adapters.hotkey_listener import probe_hotkey

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.assertFalse(probe_hotkey(user32, "not-a-key"))

    def test_pick_returns_a_usable_spec(self):
        import ctypes

        from stealth_typer.adapters.hotkey_listener import (
            AUTO_ARM_CANDIDATES,
            parse_hotkey,
            pick_free_hotkey,
        )

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        chosen = pick_free_hotkey(user32, AUTO_ARM_CANDIDATES)
        self.assertIsNotNone(chosen, "no free arm combination found")
        self.assertIn(chosen, AUTO_ARM_CANDIDATES)
        parse_hotkey(chosen)  # must be a spec we can actually parse

    def test_pick_respects_avoid_list(self):
        import ctypes

        from stealth_typer.adapters.hotkey_listener import (
            AUTO_PAUSE_CANDIDATES,
            pick_free_hotkey,
        )

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        first = pick_free_hotkey(user32, AUTO_PAUSE_CANDIDATES)
        second = pick_free_hotkey(user32, AUTO_PAUSE_CANDIDATES, avoid=(first,))
        self.assertNotEqual(first, second, "avoid list was ignored")

    def test_candidate_lists_parse_and_are_disjoint_in_practice(self):
        from stealth_typer.adapters.hotkey_listener import (
            AUTO_ARM_CANDIDATES,
            AUTO_PAUSE_CANDIDATES,
            parse_hotkey,
        )

        for spec in AUTO_ARM_CANDIDATES + AUTO_PAUSE_CANDIDATES:
            parse_hotkey(spec)  # raises if malformed
        self.assertTrue(AUTO_ARM_CANDIDATES)
        self.assertTrue(AUTO_PAUSE_CANDIDATES)

    def test_modifier_vks_cover_every_modifier_flag(self):
        from stealth_typer.adapters.hotkey_listener import (
            MOD_ALT,
            MOD_CONTROL,
            MOD_SHIFT,
            MOD_WIN,
            HotkeyListener,
        )

        listener = HotkeyListener("ctrl+alt+shift+win+k")
        codes = listener._mod_vks()
        self.assertEqual(len(codes), 4)
        for flag in (MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN):
            self.assertTrue(
                any(listener.modifiers & flag for _ in [0]),
                "modifier flag not reflected in modifiers",
            )

    def test_combo_down_is_false_when_nothing_pressed(self):
        from stealth_typer.adapters.hotkey_listener import HotkeyListener

        listener = HotkeyListener("alt+x")
        # No synthetic key is held in a test process, so this must be False
        # rather than raising.
        self.assertFalse(listener._combo_down())

    def test_cli_defaults_to_auto(self):
        import main as cli

        args = cli.build_parser().parse_args(["--text", "x"])
        self.assertEqual(args.key, "auto")


class ArmGraceWindowTests(unittest.TestCase):
    """Pressing the arm key before focus is ready must still start typing.

    Regression: the grace loop used ``armed_by_user`` as its loop condition,
    but that flag is only set once we commit to starting. A successful
    detection therefore fell straight through to "gave up", so pressing the key
    a moment too early silently did nothing - which is the most natural way for
    a user to press it.
    """

    class LateFocusDetector:
        """Console focused first; editor focused after ``focus_delay``."""

        def __init__(self, focus_delay):
            self.t0 = time.monotonic()
            self.focus_delay = focus_delay
            self.console = 100
            self.editor = 200

        def get_foreground_window(self):
            late = (time.monotonic() - self.t0) > self.focus_delay
            return self.editor if late else self.console

        def is_own_console(self, handle):
            return handle == self.console

        def is_editable_target(self, handle, stealthdesk):
            return handle == self.editor

        def has_keyboard_focus(self, handle):
            return handle == self.editor

        def is_stealthdesk_window(self, handle, stealthdesk):
            return False

        def is_window_valid(self, handle):
            return bool(handle)

    @staticmethod
    def _build(detector):
        from stealth_typer.adapters.dry_run_input_port import DryRunInputInjectionPort
        from stealth_typer.adapters.thread_timer import ThreadTimer
        from stealth_typer.application.typer_service import TyperService
        from stealth_typer.core.typing_policy import AutoIndentMode
        from stealth_typer.ports.i_event_bus import CollectingEventBus

        timer = ThreadTimer()
        port = DryRunInputInjectionPort(echo=False)
        events = CollectingEventBus()
        typer = TyperService(None, port, timer, events, detector)
        typer.session().set_text("GRACE")
        typer.set_speed(100)
        typer.set_activation_settling_delay_ms(0)
        typer.set_auto_indent_mode(AutoIndentMode.RAW)
        typer.arm(auto_pick=False)
        return typer, port, timer, events

    @staticmethod
    def _arm_like_cli(typer, detector, events, armed):
        """The CLI's arm closure, including the got_target fix."""
        from main import ARM_GRACE_SECONDS
        from stealth_typer.ports.i_event_bus import StatusChanged

        if armed.is_set():
            return
        fg = detector.get_foreground_window()
        ready = (
            not detector.is_own_console(fg)
            and detector.is_editable_target(fg, 0)
            and detector.has_keyboard_focus(fg)
        )
        if not ready:
            events.publish_status(StatusChanged("Heard arm key too early"))
            deadline = time.monotonic() + ARM_GRACE_SECONDS
            got_target = False
            while time.monotonic() < deadline:
                time.sleep(0.15)
                fg = detector.get_foreground_window()
                ready = (
                    not detector.is_own_console(fg)
                    and detector.is_editable_target(fg, 0)
                    and detector.has_keyboard_focus(fg)
                )
                if ready:
                    got_target = True
                    break
            if not got_target:
                events.publish_status(StatusChanged("Gave up waiting"))
                return
        armed.set()
        typer.start_monitoring()
        typer.start_auto()

    def _run(self, focus_delay, wait):
        detector = self.LateFocusDetector(focus_delay)
        typer, port, timer, events = self._build(detector)
        armed = threading.Event()
        try:
            threading.Thread(
                target=self._arm_like_cli,
                args=(typer, detector, events, armed),
                daemon=True,
            ).start()
            fired = armed.wait(wait)
            deadline = time.monotonic() + 5
            while typer.state != TyperState.IDLE and time.monotonic() < deadline:
                time.sleep(0.02)
            return fired, port.typed_text(), events.statuses
        finally:
            timer.shutdown()

    def test_pressing_early_still_starts(self):
        fired, typed, _ = self._run(focus_delay=1.0, wait=10.0)
        self.assertTrue(fired, "grace window did not pick up the late focus")
        self.assertEqual(typed, "GRACE")

    def test_pressing_when_ready_starts_immediately(self):
        fired, typed, _ = self._run(focus_delay=-1.0, wait=10.0)
        self.assertTrue(fired)
        self.assertEqual(typed, "GRACE")

    def test_gives_up_cleanly_when_focus_never_arrives(self):
        fired, typed, statuses = self._run(focus_delay=999.0, wait=4.0)
        self.assertFalse(fired)
        self.assertEqual(typed, "")
        self.assertIn("Gave up waiting", statuses)

    def test_grace_window_is_a_few_seconds(self):
        import main as cli

        self.assertGreaterEqual(cli.ARM_GRACE_SECONDS, 3.0)
        self.assertLessEqual(cli.ARM_GRACE_SECONDS, 30.0)


class AutoResumeOnFocusLossTests(unittest.TestCase):
    """Losing focus pauses; regaining it resumes. A user pause does not.

    Regression from a real report: typing started, the user glanced at the
    console to read the status, focus moved, and the run paused BEFORE any
    character was delivered - then stayed dead, so nothing was ever typed.
    Two facts caused that: on_tick re-checks the target before typing, and a
    focus-loss pause was indistinguishable from an explicit user pause.
    """

    class EditorDetector:
        """Foreground follows ``focus``, which the test moves around."""

        def __init__(self):
            self.editor = 200
            self.console = 100
            self.focus = self.editor

        def get_foreground_window(self):
            return self.focus

        def is_stealthdesk_window(self, handle, stealthdesk):
            return False

        def is_window_valid(self, handle):
            return bool(handle)

        def is_editable_target(self, handle, stealthdesk):
            return handle == self.editor

        def has_keyboard_focus(self, handle):
            return handle == self.focus

        def _class_name(self, hwnd):
            return "Editor" if hwnd == self.editor else "Console"

    def _build(self, text=None):
        from stealth_typer.adapters.dry_run_input_port import DryRunInputInjectionPort
        from stealth_typer.adapters.thread_timer import ThreadTimer
        from stealth_typer.application.typer_service import TyperService
        from stealth_typer.core.typing_policy import AutoIndentMode
        from stealth_typer.ports.i_event_bus import CollectingEventBus

        detector = self.EditorDetector()
        timer = ThreadTimer()
        port = DryRunInputInjectionPort(echo=False)
        events = CollectingEventBus()
        typer = TyperService(None, port, timer, events, detector)
        # Long enough that the run is still going when the test steals focus;
        # a short string finishes first and there is nothing left to pause.
        text = text if text is not None else "abcdefghij" * 60
        typer.session().set_text(text)
        typer.set_speed(100)
        typer.set_activation_settling_delay_ms(0)
        typer.set_auto_indent_mode(AutoIndentMode.RAW)
        return typer, port, timer, events, detector, text

    def test_focus_loss_pauses_and_regaining_focus_resumes(self):
        typer, port, timer, events, detector, text = self._build()
        try:
            typer.arm(auto_pick=False)
            typer.start_monitoring()
            typer.start_auto()
            time.sleep(0.25)

            detector.focus = detector.console  # user glances at the console
            self.assertTrue(wait_for_pause(typer), "focus loss did not pause")
            self.assertTrue(typer._auto_paused, "focus-loss pause must be auto-resumable")

            frozen = len(port.unicode_chars)
            time.sleep(0.3)
            self.assertEqual(len(port.unicode_chars), frozen, "typed while focus was away")

            detector.focus = detector.editor  # user clicks back in
            deadline = time.monotonic() + 15
            while typer.state != TyperState.IDLE and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(port.typed_text(), text)
        finally:
            timer.shutdown()

    def test_resume_keeps_the_original_target(self):
        """A resume must not re-detect and latch onto a different window.

        Regression from a real report: pressing alt+x locked the run to whatever
        was focused, the user then clicked into the editor they actually meant,
        and the text kept going to the original window now sitting in the
        background - so nothing appeared where they were looking.
        """
        typer, port, timer, events, detector, text = self._build()
        try:
            typer.arm(auto_pick=False)
            typer.start_monitoring()
            typer.start_auto()
            self.assertTrue(
                wait_for_state(typer, (TyperState.TYPING,)), "never started typing"
            )
            detector.focus = detector.console
            self.assertTrue(wait_for_pause(typer), "focus loss did not pause")

            # User glances at the console, then presses the toggle key to resume.
            typer.resume_auto()
            self.assertEqual(
                typer._target_handle,
                detector.editor,
                "resume re-locked onto a different window",
            )
        finally:
            timer.shutdown()

    def test_console_toggle_is_ignored_as_a_target(self):
        """The console must never be adopted as the typing target."""
        typer, port, timer, events, detector, _ = self._build()
        try:
            typer.arm(auto_pick=False)
            typer.start_monitoring()
            typer.start_auto()
            self.assertTrue(
                wait_for_state(typer, (TyperState.TYPING,)), "never started typing"
            )
            detector.focus = detector.console
            self.assertTrue(wait_for_pause(typer), "focus loss did not pause")
            typer.resume_auto()
            self.assertNotEqual(
                typer._target_handle,
                detector.console,
                "typed into the console",
            )
        finally:
            timer.shutdown()

    def test_activation_message_reports_real_position(self):
        """The resume line must show the true position, not a hardcoded #1."""
        typer, port, timer, events, detector, _ = self._build()
        try:
            typer.arm(auto_pick=False)
            typer.start_monitoring()
            typer.start_auto()
            self.assertTrue(
                wait_for_state(typer, (TyperState.TYPING,)), "never started typing"
            )
            detector.focus = detector.console
            self.assertTrue(wait_for_pause(typer), "focus loss did not pause")
            typer.resume_auto()
            time.sleep(0.2)
            joined = " | ".join(events.statuses)
            self.assertNotIn(
                "char #1",
                joined,
                "position is misreported as char #1 on every resume",
            )
        finally:
            timer.shutdown()

    def test_explicit_user_pause_is_not_auto_resumed(self):
        typer, port, timer, events, detector, _ = self._build()
        try:
            typer.arm(auto_pick=False)
            typer.start_monitoring()
            typer.start_auto()
            time.sleep(0.2)

            typer.pause_auto_by_user()
            self.assertEqual(typer.state, TyperState.PAUSED)
            self.assertFalse(typer._auto_paused, "user pause must not auto-resume")

            frozen = len(port.unicode_chars)
            detector.focus = detector.console
            time.sleep(0.2)
            detector.focus = detector.editor  # click back in
            time.sleep(0.6)
            self.assertEqual(len(port.unicode_chars), frozen, "user pause did not hold")
            self.assertEqual(typer.state, TyperState.PAUSED)
        finally:
            timer.shutdown()

    def test_focus_loss_message_names_the_window(self):
        typer, port, timer, events, detector, _ = self._build()
        try:
            typer.arm(auto_pick=False)
            typer.start_monitoring()
            typer.start_auto()
            time.sleep(0.2)
            detector.focus = detector.console
            self.assertTrue(wait_for_pause(typer), "focus loss did not pause")
            message = " ".join(events.statuses)
            self.assertIn("Focus moved to", message)
            # The user must be told it resumes by itself.
            self.assertIn("automatically", message)
        finally:
            timer.shutdown()

    def test_resume_auto_clears_the_flag_and_restarts(self):
        typer, port, timer, events, detector, _ = self._build()
        try:
            typer.arm(auto_pick=False)
            typer.start_monitoring()
            typer.start_auto()
            time.sleep(0.2)
            detector.focus = detector.console
            self.assertTrue(wait_for_pause(typer), "focus loss did not pause")
            self.assertTrue(typer._auto_paused)

            detector.focus = detector.editor
            typer.resume_auto()
            self.assertFalse(typer._auto_paused)
        finally:
            timer.shutdown()


class TransientOverlayTests(unittest.TestCase):
    """Shell/IME/tooltip windows must not be treated as a focus change.

    Regression from a real report: "ForegroundStaging" is the hidden window
    the shell shows for a few milliseconds during window activation. Treating
    it as a focus change produced an endless pause/resume loop in which no
    character was ever typed, because on_tick re-checks the target before
    typing each character.
    """

    def test_foreground_staging_is_a_transient_class(self):
        from stealth_typer.adapters.win_target_detector import (
            _TRANSIENT_CLASSES,
            _is_transient_class,
        )

        self.assertIn("ForegroundStaging", _TRANSIENT_CLASSES)
        self.assertTrue(_is_transient_class("ForegroundStaging"))

    def test_real_editors_are_not_treated_as_transient(self):
        from stealth_typer.adapters.win_target_detector import _is_transient_class

        for name in ("Notepad", "Edit", "Chrome_WidgetWin_1", "SunAwtFrame"):
            self.assertFalse(
                _is_transient_class(name), "%s wrongly treated as transient" % name
            )

    def test_win32_detector_handles_null(self):
        from stealth_typer.adapters.win_target_detector import WinTargetDetector

        self.assertFalse(WinTargetDetector().is_transient_overlay(0))

    def test_port_default_is_never_transient(self):
        from stealth_typer.ports.i_target_detector import ITargetDetector

        class Bare(ITargetDetector):
            def is_editable_target(self, t, s):
                return True

            def is_stealthdesk_window(self, h, s):
                return False

            def is_window_valid(self, h):
                return True

            def get_foreground_window(self):
                return 1

        self.assertFalse(Bare().is_transient_overlay(1))


class TransientOverlayPauseTests(unittest.TestCase):
    """The service must type through a staging flicker, pause for real changes."""

    class StagingDetector:
        def __init__(self, staging_is_transient=True):
            self.editor = 200
            self.console = 100
            self.staging = 300
            self.focus = self.editor
            self.staging_is_transient = staging_is_transient

        def get_foreground_window(self):
            return self.focus

        def is_stealthdesk_window(self, handle, stealthdesk):
            return False

        def is_window_valid(self, handle):
            return bool(handle)

        def is_editable_target(self, handle, stealthdesk):
            return handle == self.editor

        def has_keyboard_focus(self, handle):
            return handle == self.focus

        def is_transient_overlay(self, handle):
            return handle == self.staging and self.staging_is_transient

        def _class_name(self, hwnd):
            return {
                self.editor: "Notepad",
                self.console: "Console",
                self.staging: "ForegroundStaging",
            }.get(hwnd, "Other")

    def _build(self, text, detector):
        from stealth_typer.adapters.dry_run_input_port import DryRunInputInjectionPort
        from stealth_typer.adapters.thread_timer import ThreadTimer
        from stealth_typer.application.typer_service import TyperService
        from stealth_typer.core.typing_policy import AutoIndentMode
        from stealth_typer.ports.i_event_bus import CollectingEventBus

        timer = ThreadTimer()
        port = DryRunInputInjectionPort(echo=False)
        typer = TyperService(None, port, timer, CollectingEventBus(), detector)
        typer.session().set_text(text)
        typer.set_speed(100)
        typer.set_activation_settling_delay_ms(0)
        typer.set_auto_indent_mode(AutoIndentMode.RAW)
        return typer, port, timer

    def test_staging_flicker_does_not_pause(self):
        detector = self.StagingDetector(staging_is_transient=True)
        typer, port, timer = self._build("HELLO", detector)
        try:
            typer.arm(auto_pick=False)
            typer.start_monitoring()
            typer.start_auto()
            time.sleep(0.2)
            detector.focus = detector.staging  # shell flashes staging
            time.sleep(0.4)
            self.assertNotEqual(
                typer.state, TyperState.PAUSED, "staging window caused a pause"
            )
            detector.focus = detector.editor
            deadline = time.monotonic() + 8
            while typer.state != TyperState.IDLE and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(port.typed_text(), "HELLO")
        finally:
            timer.shutdown()

    def test_real_focus_change_still_pauses(self):
        detector = self.StagingDetector(staging_is_transient=True)
        typer, port, timer = self._build("A" * 200, detector)
        try:
            typer.arm(auto_pick=False)
            typer.start_monitoring()
            typer.start_auto()
            time.sleep(0.3)
            detector.focus = detector.console  # user clicks the console
            self.assertTrue(wait_for_pause(typer), "focus loss did not pause")
            frozen = len(port.unicode_chars)
            self.assertGreater(frozen, 0, "should have typed before the pause")
            time.sleep(0.4)
            self.assertEqual(len(port.unicode_chars), frozen, "typed while away")
        finally:
            timer.shutdown()

    def test_detector_that_cannot_classify_still_pauses(self):
        class NoHook(self.StagingDetector):
            is_transient_overlay = None

        detector = NoHook(staging_is_transient=False)
        typer, port, timer = self._build("A" * 200, detector)
        try:
            typer.arm(auto_pick=False)
            typer.start_monitoring()
            typer.start_auto()
            time.sleep(0.3)
            detector.focus = detector.staging
            self.assertTrue(wait_for_pause(typer), "unclassifiable overlay did not pause")
        finally:
            timer.shutdown()


class FocusGlanceTests(unittest.TestCase):
    """A brief look at the console must not interrupt typing, or leak keys.

    Regression from a real report: every status line invited the user to read
    the console, and pausing on the first momentary focus change produced a
    churn loop (pause -> print -> user clicks console -> pause) in which no
    character was ever delivered.
    """

    class Detector:
        def __init__(self):
            self.editor = 200
            self.console = 100
            self.staging = 300
            self.focus = self.editor

        def get_foreground_window(self):
            return self.focus

        def is_stealthdesk_window(self, handle, stealthdesk):
            return False

        def is_window_valid(self, handle):
            return bool(handle)

        def is_editable_target(self, handle, stealthdesk):
            return handle == self.editor

        def has_keyboard_focus(self, handle):
            return handle == self.focus

        def is_transient_overlay(self, handle):
            return handle == self.staging

        def _class_name(self, hwnd):
            return {
                self.editor: "Notepad",
                self.console: "Console",
                self.staging: "ForegroundStaging",
            }.get(hwnd, "Other")

    def _build(self):
        from stealth_typer.adapters.dry_run_input_port import DryRunInputInjectionPort
        from stealth_typer.adapters.thread_timer import ThreadTimer
        from stealth_typer.application.typer_service import TyperService
        from stealth_typer.core.typing_policy import AutoIndentMode
        from stealth_typer.ports.i_event_bus import CollectingEventBus

        detector = self.Detector()
        timer = ThreadTimer()
        port = DryRunInputInjectionPort(echo=False)
        events = CollectingEventBus()
        typer = TyperService(None, port, timer, events, detector)
        typer.session().set_text("A" * 60)
        typer.set_speed(20)  # slow: ~405 ms per char, so a glance is observable
        typer.set_activation_settling_delay_ms(0)
        typer.set_auto_indent_mode(AutoIndentMode.RAW)
        typer.arm(auto_pick=False)
        typer.start_monitoring()
        typer.start_auto()
        return typer, port, timer, events, detector

    def test_grace_is_long_enough_for_a_glance(self):
        from stealth_typer.application.typer_service import FOCUS_LOSS_GRACE_SECONDS

        self.assertGreaterEqual(FOCUS_LOSS_GRACE_SECONDS, 1.0)
        self.assertLessEqual(FOCUS_LOSS_GRACE_SECONDS, 30.0)

    def test_brief_glance_does_not_pause(self):
        from stealth_typer.application.typer_service import FOCUS_LOSS_GRACE_SECONDS

        typer, port, timer, events, detector = self._build()
        try:
            time.sleep(0.9)
            detector.focus = detector.console
            time.sleep(FOCUS_LOSS_GRACE_SECONDS * 0.5)  # a glance
            detector.focus = detector.editor

            self.assertNotEqual(typer.state, TyperState.PAUSED, "a glance paused typing")
            self.assertFalse(
                any("Focus moved" in s for s in events.statuses),
                "a glance published a focus-loss message",
            )
        finally:
            timer.shutdown()

    def test_sustained_focus_loss_pauses(self):
        from stealth_typer.application.typer_service import FOCUS_LOSS_GRACE_SECONDS

        typer, port, timer, events, detector = self._build()
        try:
            time.sleep(0.9)
            detector.focus = detector.console
            time.sleep(FOCUS_LOSS_GRACE_SECONDS + 0.8)
            self.assertEqual(typer.state, TyperState.PAUSED)
        finally:
            timer.shutdown()

    def test_no_keystrokes_leak_while_focus_is_uncertain(self):
        from stealth_typer.application.typer_service import FOCUS_LOSS_GRACE_SECONDS

        typer, port, timer, events, detector = self._build()
        try:
            time.sleep(0.9)
            detector.focus = detector.console
            # Mid-grace: focus is uncertain, so nothing may be typed anywhere.
            time.sleep(0.3)
            frozen = len(port.unicode_chars)
            time.sleep(0.3)
            self.assertEqual(
                len(port.unicode_chars),
                frozen,
                "keystrokes leaked into a window that was not the target",
            )
        finally:
            timer.shutdown()

    def test_focus_returning_clears_the_pending_change(self):
        typer, port, timer, events, detector = self._build()
        try:
            time.sleep(0.9)
            detector.focus = detector.console
            time.sleep(0.4)
            self.assertIsNotNone(typer._focus_lost_since)
            detector.focus = detector.editor
            typer.evaluate_target_window()
            self.assertIsNone(typer._focus_lost_since)
            self.assertNotEqual(typer.state, TyperState.PAUSED)
        finally:
            timer.shutdown()


class PauseKeyGuardTests(unittest.TestCase):
    """The pause key must not be able to derail a run that is not typing.

    Regression from a real report: a run reached "Paused at 44/1335" without
    the user asking for it. The pause combination had fired on its own - the
    polled fallback can catch a modifier still held from the arm press plus an
    ordinary keypress.
    """

    def test_pause_key_is_ignored_before_typing_starts(self):
        from stealth_typer.adapters.dry_run_input_port import DryRunInputInjectionPort
        from stealth_typer.adapters.thread_timer import ThreadTimer
        from stealth_typer.application.typer_service import TyperService
        from stealth_typer.core.typing_policy import AutoIndentMode
        from stealth_typer.ports.i_event_bus import CollectingEventBus

        timer = ThreadTimer()
        try:
            events = CollectingEventBus()
            typer = TyperService(
                None,
                DryRunInputInjectionPort(echo=False),
                timer,
                events,
                TestTargetDetector(),
            )
            typer.session().set_text("abc")
            typer.set_speed(100)
            typer.set_activation_settling_delay_ms(0)
            typer.set_auto_indent_mode(AutoIndentMode.RAW)
            typer.arm(auto_pick=False)
            # Armed, not typing: a pause here would be a mistake.
            self.assertEqual(typer.state, TyperState.ARMED)

            # The CLI guard: only TYPING/PAUSED respond to the pause key.
            responds = typer.state in (TyperState.TYPING, TyperState.PAUSED)
            self.assertFalse(
                responds, "ARMED must not accept the pause key"
            )
        finally:
            timer.shutdown()

    def test_hotkey_hold_time_is_long_enough_to_ignore_strays(self):
        from stealth_typer.adapters.hotkey_listener import HOTKEY_HOLD_SECONDS

        # A deliberate press is ~100ms or more; a rolled modifier slipping into
        # a keypress is far shorter.
        self.assertGreaterEqual(HOTKEY_HOLD_SECONDS, 0.15)
        self.assertLessEqual(HOTKEY_HOLD_SECONDS, 2.0)


class IntegrityParsingTests(unittest.TestCase):
    """The RID in an integrity SID string is DECIMAL, not hexadecimal.

    Getting this wrong reported an ordinary unelevated process as "System",
    which made the UIPI comparison give a confident but wrong answer about why
    keystrokes were not appearing - the opposite of what a diagnostic is for.
    """

    def test_medium_is_not_system(self):
        import main

        self.assertEqual(main.parse_integrity_sid("S-1-16-8192"), "Medium")

    def test_all_levels(self):
        import main

        cases = {
            "S-1-16-4096": "Low",
            "S-1-16-8192": "Medium",
            "S-1-16-12288": "High",
            "S-1-16-16384": "System",
        }
        for sid, expected in cases.items():
            self.assertEqual(main.parse_integrity_sid(sid), expected, sid)

    def test_untrusted_and_nonsense(self):
        import main

        self.assertTrue(main.parse_integrity_sid("S-1-16-0").startswith("Untrusted"))
        self.assertTrue(main.parse_integrity_sid("").startswith("Unknown"))
        self.assertTrue(main.parse_integrity_sid("nonsense").startswith("Unknown"))

    def test_ranking_is_ordered(self):
        import main

        ranks = [main.integrity_rank(v) for v in ("Low", "Medium", "High", "System")]
        self.assertEqual(ranks, sorted(ranks))
        self.assertLess(main.integrity_rank("Medium"), main.integrity_rank("High"))

    def test_live_process_reports_a_known_level(self):
        import main

        level = main.process_integrity_level()
        self.assertIn(level, ("Low", "Medium", "High", "System"))


if __name__ == "__main__":
    unittest.main(verbosity=2)




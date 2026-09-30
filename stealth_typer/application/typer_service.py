"""Port of StealthDesk ``src/application/services/typer_service.{h,cpp}``.

The auto-typing state machine, kept as close to the original as Python allows:

    Idle -> Armed -> ActivationSettling -> Typing <-> Paused -> Idle

* ``arm()`` loads the clipboard into a :class:`TypingSession` and asks the user
  to click a target editor.
* The 100 ms monitor timer picks up an editable foreground window and moves to
  ``ActivationSettling``, which waits out ``activation_settling_delay_ms`` so
  focus (and any ``WM_CHAR``-generating focus change) has settled.
* ``Typing`` emits one codepoint per ``ITimer`` tick using the configured
  ``ITypingTimingModel``. Losing the target window pauses typing instead of
  typing into whatever happens to be in front.
* Smart IDE auto-indent skips leading whitespace after a newline, because the
  IDE already inserted its own indentation.

Every OS interaction goes through the ports, so the class is testable and runs
unchanged against the Win32 adapters or the dry-run ones.
"""

import time
from typing import Optional

from ..core.timing_model import (
    FixedTimingModel,
    HumanizationLevel,
    HumanLikeTimingModel,
    ITypingTimingModel,
)
from ..core.typing_policy import AutoIndentMode, clamp_speed
from ..core.typing_session import TypingSession
from ..domain.typer_state import TyperState
from ..ports.i_clipboard_port import IClipboardPort
from ..ports.i_event_bus import IEventBus, StatusChanged
from ..ports.i_input_injection_port import IInputInjectionPort
from ..ports.i_target_detector import ITargetDetector
from ..ports.i_timer import ITimer
from .i_typer_service import ITyperService

VK_RETURN = 0x0D
VK_TAB = 0x09

# Win32 error codes the status messages distinguish.
ERROR_ACCESS_DENIED = 5
ERROR_NOT_SUPPORTED = 50

# How long a focus change must persist before it is treated as real and
# typing pauses. Long enough that glancing at the console - the natural thing
# to do while watching a run - does not interrupt it.
FOCUS_LOSS_GRACE_SECONDS = 1.5


class TyperService(ITyperService):
    def __init__(
        self,
        clipboard: Optional[IClipboardPort],
        input_port: Optional[IInputInjectionPort],
        timer: Optional[ITimer],
        events: Optional[IEventBus],
        target_detector: Optional[ITargetDetector] = None,
    ) -> None:
        self._clipboard = clipboard
        self._input = input_port
        self._timer = timer
        self._events = events
        self._target_detector = target_detector
        self._session = TypingSession()
        self._timing_model: ITypingTimingModel = FixedTimingModel()

        self._state = TyperState.IDLE
        self._stealthdesk_handle = 0
        self._target_handle = 0
        self._speed = 50

        self._auto_indent_mode = AutoIndentMode.SMART_IDE
        self._resolved_auto_indent_mode = AutoIndentMode.SMART_IDE
        self._activation_settling_delay_ms = 250
        self._settling_deadline: Optional[float] = None
        self._pending_indent_to_skip = 0
        # True when the current pause came from focus/target loss rather than
        # the user, which is what makes auto-resume safe.
        self._auto_paused = False
        # When a focus change was first noticed, for the persistence grace.
        self._focus_lost_since = None
        # Set while auto-resuming, so the "typing started" line is not reprinted.
        self._resuming_quietly = False

        self.k_timer_id = 200
        self.k_monitor_timer_id = 201
        self.k_settling_timer_id = 202

    # -- configuration -----------------------------------------------------
    def set_timing_model(self, model: ITypingTimingModel) -> None:
        if model is not None:
            self._timing_model = model

    def set_humanization_level(self, level: HumanizationLevel) -> None:
        if level == HumanizationLevel.OFF:
            self._timing_model = FixedTimingModel()
            return
        if self._timing_model is None or self._timing_model.humanization_level == HumanizationLevel.OFF:
            self._timing_model = HumanLikeTimingModel(level)
        else:
            self._timing_model.set_humanization_level(level)

    @property
    def humanization_level(self) -> HumanizationLevel:
        return self._timing_model.humanization_level if self._timing_model else HumanizationLevel.OFF

    def set_speed(self, speed: int) -> None:
        next_speed = clamp_speed(speed)
        if next_speed == self._speed:
            return
        self._speed = next_speed
        if self._state == TyperState.TYPING and self._timer:
            peek = self._session.peek_codepoint() or 0
            delay_ms = self._timing_model.next_delay_ms(peek, 0, self._speed)
            self._timer.start_periodic(self.k_timer_id, delay_ms, self.on_tick)
        self._publish("Speed: %d%%" % self._speed)

    @property
    def speed(self) -> int:
        return self._speed

    @property
    def state(self) -> TyperState:
        return self._state

    @property
    def position(self) -> int:
        return self._session.position

    @property
    def total_codepoints(self) -> int:
        return self._session.total_codepoints

    def set_target_hwnd(self, hwnd: int) -> None:
        self._target_handle = int(hwnd or 0)

    def set_stealthdesk_hwnd(self, hwnd: int) -> None:
        self._stealthdesk_handle = int(hwnd or 0)

    def set_target_detector(self, detector: ITargetDetector) -> None:
        self._target_detector = detector

    @property
    def auto_indent_mode(self) -> AutoIndentMode:
        return self._auto_indent_mode

    def set_auto_indent_mode(self, mode: AutoIndentMode) -> None:
        self._auto_indent_mode = mode

    @property
    def effective_auto_indent_mode(self) -> AutoIndentMode:
        """The mode actually in force, with ``AUTO`` already resolved.

        In ``AUTO`` mode this is decided once per session when the target
        window is selected, so a mid-run target change does not silently flip
        the indentation behaviour underneath a partially typed document.
        """
        if self._auto_indent_mode != AutoIndentMode.AUTO:
            return self._auto_indent_mode
        return self._resolved_auto_indent_mode

    def _resolve_auto_indent_for_target(self, handle: int) -> None:
        """Ask the detector whether ``handle`` indents by itself.

        Falls back to ``SMART_IDE`` when the answer is unknown, matching the
        original's default: over-skipping is recoverable by deleting spaces,
        whereas typing them into an auto-indenting editor leaves a visible mess.
        """
        if self._auto_indent_mode != AutoIndentMode.AUTO:
            self._resolved_auto_indent_mode = self._auto_indent_mode
            return

        answer = None
        if self._target_detector:
            try:
                answer = self._target_detector.is_auto_indenting_editor(handle)
            except Exception:
                answer = None

        if answer is True:
            self._resolved_auto_indent_mode = AutoIndentMode.SMART_IDE
        elif answer is False:
            self._resolved_auto_indent_mode = AutoIndentMode.RAW
        else:
            self._resolved_auto_indent_mode = AutoIndentMode.SMART_IDE

    def _describe_effective_indent_mode(self) -> str:
        mode = self.effective_auto_indent_mode
        if mode == AutoIndentMode.SMART_IDE:
            return "smart (editor auto-indents, skipping source indentation)"
        return "raw (typing source indentation exactly)"

    @property
    def activation_settling_delay_ms(self) -> int:
        return self._activation_settling_delay_ms

    def set_activation_settling_delay_ms(self, ms: int) -> None:
        self._activation_settling_delay_ms = int(ms)

    def session(self) -> TypingSession:
        """The live session, so a CLI can load text without a clipboard."""
        return self._session

    # -- refusals ----------------------------------------------------------
    def input_refusal(self) -> Optional[str]:
        """Why typing is refused right now, or ``None`` when it can proceed.

        Covers both a missing port and a port that is present and declines, so
        callers do not need to distinguish them.
        """
        if self._input is None:
            return "Typing unavailable: no input driver"
        if not self._input.is_available():
            why = self._input.unavailable_reason()
            return why if why else "Typing unavailable: no input driver"
        return None

    def _publish(self, text: str) -> None:
        if self._events:
            self._events.publish_status(StatusChanged(text))

    # -- lifecycle ---------------------------------------------------------
    def arm(self, auto_pick: bool = True) -> None:
        """Load the text and go ARMED.

        ``auto_pick=False`` stops short of looking for a target window. The CLI
        uses it so a run waits for the user to click their editor and press the
        arm hotkey, rather than grabbing whatever happened to be in the
        foreground at startup.
        """
        # Refused before reading the clipboard: arming a session that can never
        # be delivered leaves the user watching a status that will never change.
        why = self.input_refusal()
        if why:
            self.reset()
            self._publish(why)
            return

        if self._clipboard:
            # Locked-down browsers empty the clipboard every ~50 ms unless the
            # exam allows it, so a copy->arm slower than one cycle reads empty.
            # Bounded retry (3 x 60 ms) also covers the natural arm->copy
            # ordering. Typing itself never touches the clipboard
            # (per-character kernel keystrokes), so this only affects arming.
            text = ""
            for _ in range(3):
                text = self._clipboard.get_text()
                if text:
                    break
                time.sleep(0.060)
            if text:
                self._session.set_text(text)

        if not self._session.text or self._session.is_complete():
            self.reset()
            self._publish("Clipboard empty or session complete")
            return

        self._state = TyperState.ARMED
        self._target_handle = 0
        self._pending_indent_to_skip = 0
        self._publish("Armed - click target editor")

        if auto_pick:
            self._start_monitor_timer()
            self.evaluate_target_window()

    def start_monitoring(self) -> None:
        """Begin looking for a target window.

        Separate from :meth:`arm` so a CLI can arm first, let the user click
        their editor, and only then start watching for that window.
        """
        self._start_monitor_timer()
        self.evaluate_target_window()

    def start_auto(self) -> None:
        if self._state == TyperState.IDLE:
            self.arm()
            return

        if self._state in (TyperState.ARMED, TyperState.PAUSED):
            if self._target_detector:
                fg = self._target_detector.get_foreground_window()
                if self._target_detector.is_editable_target(fg, self._stealthdesk_handle):
                    self._target_handle = fg
                    self._enter_activation_settling()
                    return
            self._publish("Target is not editable - click a text field/editor")

    def pause_auto(self) -> None:
        if self._state in (
            TyperState.TYPING,
            TyperState.ARMED,
            TyperState.ACTIVATION_SETTLING,
        ):
            self._state = TyperState.PAUSED

        if self._timer:
            self._timer.stop(self.k_timer_id)
            self._timer.stop(self.k_settling_timer_id)

        self._publish("Auto paused")

    def resume_auto(self) -> None:
        """Resume from an auto-pause, or start fresh if nothing is paused.

        On resume the target is NOT re-detected. Re-running detection here is
        what made a resume behave like a restart: it latched onto whatever was
        foreground at that instant, which during a pause is usually the console
        the user just clicked to read the status. Keeping the existing target
        means a resume continues into the same document.
        """
        self._auto_paused = False
        if self._state == TyperState.PAUSED:
            if self._target_handle and self._target_detector:
                fg = self._target_detector.get_foreground_window()
                if (
                    fg
                    and self._target_detector.is_window_valid(self._target_handle)
                    and self._target_detector.is_editable_target(
                        self._target_handle, self._stealthdesk_handle
                    )
                ):
                    self._state = TyperState.ACTIVATION_SETTLING
                    self._settling_deadline = time.monotonic() + (
                        self._activation_settling_delay_ms / 1000.0
                    )
                    if self._timer:
                        self._timer.start_once(
                            self.k_settling_timer_id,
                            self._activation_settling_delay_ms,
                            self.on_activation_settled,
                        )
                    self._resuming_quietly = True
                    return
            # The old target is gone or no longer editable: fall back to a full
            # start, which re-detects and tells the user what it picked.
            self.start_auto()
            return
        self.start_auto()

    def pause_auto_by_user(self) -> None:
        """An explicit pause request: holds until the user resumes.

        Clears the auto-resume flag so that glancing away from the editor later
        cannot silently undo a pause the user asked for.
        """
        self._auto_paused = False
        self.pause_auto()

    def reset(self) -> None:
        self.pause_auto()
        self._stop_monitor_timer()
        self._state = TyperState.IDLE
        self._target_handle = 0
        self._pending_indent_to_skip = 0
        self._session.clear()
        self._publish("Session reset")

    def inject_vk(self, vk: int) -> None:
        # The hotkey path. Refused, not attempted: the caller's action (arrow
        # key in the editor) must not silently do nothing when there is no way
        # to deliver it.
        if self.input_refusal():
            return
        self._input.inject_virtual_key(vk, False)
        self._input.inject_virtual_key(vk, True)

    # -- timers ------------------------------------------------------------
    def _start_monitor_timer(self) -> None:
        if self._timer:
            self._timer.start_periodic(self.k_monitor_timer_id, 100, self.evaluate_target_window)

    def _stop_monitor_timer(self) -> None:
        if self._timer:
            self._timer.stop(self.k_monitor_timer_id)

    def _enter_activation_settling(self, quiet: bool = False) -> None:
        # AUTO is resolved here, once per target selection, so the answer is
        # published with the status the user actually sees. Quiet suppresses
        # the whole block: an auto-resume after a brief glance must not reprint
        # three lines, because those lines are what tempt the user to click the
        # console again and start the pause/resume churn all over again.
        if not quiet:
            self._resolve_auto_indent_for_target(self._target_handle)
        self._state = TyperState.ACTIVATION_SETTLING
        self._settling_deadline = time.monotonic() + (self._activation_settling_delay_ms / 1000.0)
        if not quiet:
            self._publish(
                "Target selected - settling focus (%dms)..." % self._activation_settling_delay_ms
            )
            if self._auto_indent_mode == AutoIndentMode.AUTO:
                self._publish(
                    "Auto-indent resolved to %s" % self._describe_effective_indent_mode()
                )
        if self._timer:
            self._timer.start_once(
                self.k_settling_timer_id,
                self._activation_settling_delay_ms,
                self.on_activation_settled,
            )
            # Name the locked target once, at the start of a run. Without this
            # the console only ever said "Target selected", leaving the user
            # guessing which of several open windows the text was bound for.
            self._publish("Locked onto %s (hwnd %d)" % (self._describe(self._target_handle), self._target_handle))

    # -- target monitoring -------------------------------------------------
    def evaluate_target_window(self) -> None:
        if not self._target_detector:
            return

        fg = self._target_detector.get_foreground_window()

        if self._state == TyperState.ARMED:
            if self._target_detector.is_editable_target(fg, self._stealthdesk_handle):
                self._target_handle = fg
                self._enter_activation_settling()
        elif self._state == TyperState.PAUSED:
            # Auto-resume: if we paused only because focus wandered away (the
            # user glanced at the console, or an overlay stole focus) and some
            # editable window now has focus, carry on by itself.
            #
            # The NEW focused window is adopted as the target rather than
            # requiring the original one back. Locking to the window that was
            # focused when the key was pressed is what made this feel broken:
            # press alt+x in one app, click into the editor you actually meant,
            # and the run stayed pinned to a window that is now in the
            # background, so nothing appeared where you were looking.
            if (
                self._auto_paused
                and fg
                and self._target_detector.is_window_valid(fg)
                and self._target_detector.is_editable_target(
                    fg, self._stealthdesk_handle
                )
            ):
                self._auto_paused = False
                self._state = TyperState.ARMED
                self._target_handle = fg
                # Quiet: the target has not changed, so re-announcing it would
                # only add console noise the user then reacts to by clicking
                # the console, which pauses typing again.
                self._enter_activation_settling(quiet=True)
                self._resuming_quietly = True
            return
        elif self._state == TyperState.ACTIVATION_SETTLING:
            if (
                not self._target_detector.is_window_valid(self._target_handle)
                or fg != self._target_handle
                or not self._target_detector.is_editable_target(fg, self._stealthdesk_handle)
            ):
                self._state = TyperState.ARMED
                if self._timer:
                    self._timer.stop(self.k_settling_timer_id)
                self._publish("Target focus lost during settling - Armed")
        elif self._state == TyperState.TYPING:
            if self._target_handle and not self._target_detector.is_window_valid(self._target_handle):
                self.pause_auto()
                self._auto_paused = False  # window is gone; resuming is pointless
                self._target_handle = 0
                self._focus_lost_since = None
                self._publish("Target closed - typing paused")
            elif (
                fg
                and fg != self._target_handle
                and not self._target_detector.is_stealthdesk_window(fg, self._stealthdesk_handle)
                and not self._is_transient(fg)
            ):
                # A focus change only counts once it has PERSISTED.
                #
                # Reading the console is a natural thing to do while a run is in
                # progress, and every status line invites it. Pausing on the
                # first momentary change turned that into a churn loop:
                # pause -> print 5 lines -> user clicks console -> pause ...
                # with no character ever delivered. So a change must hold for
                # FOCUS_LOSS_GRACE_SECONDS before it is treated as real. While
                # it is undecided on_tick withholds keystrokes rather than
                # sending them to the wrong window.
                if self._focus_lost_since is None:
                    self._focus_lost_since = time.monotonic()
                    return
                if (time.monotonic() - self._focus_lost_since) < FOCUS_LOSS_GRACE_SECONDS:
                    return
                self.pause_auto()
                self._auto_paused = True  # resumes by itself if focus comes back
                self._focus_lost_since = None
                self._publish(
                    "Focus moved to %s - paused. Click back into your editor "
                    "and typing resumes automatically." % self._describe(fg)
                )
            elif self._target_handle and not self._target_detector.is_editable_target(
                self._target_handle, self._stealthdesk_handle
            ):
                self.pause_auto()
                self._auto_paused = False
                self._focus_lost_since = None
                self._publish("Target lost editability - typing paused")
            else:
                # Focus is on the target again: nothing was ever wrong.
                self._focus_lost_since = None

    def _describe(self, handle: int) -> str:
        """Human-readable name for a window handle, for status messages."""
        try:
            name = self._target_detector._class_name(handle)
            return name or "another window"
        except Exception:
            return "another window"

    def _is_transient(self, handle: int) -> bool:
        """Whether ``handle`` is a shell/IME/tooltip window to ignore.

        Guarded because the hook is optional: a detector that does not
        implement it keeps the older, stricter behaviour.
        """
        checker = getattr(self._target_detector, "is_transient_overlay", None)
        if checker is None:
            return False
        try:
            return bool(checker(handle))
        except Exception:
            return False

    def on_activation_settled(self) -> None:
        if self._state != TyperState.ACTIVATION_SETTLING:
            return

        now = time.monotonic()
        if (
            self._activation_settling_delay_ms > 0
            and self._settling_deadline is not None
            and now < self._settling_deadline
        ):
            remaining_ms = int((self._settling_deadline - now) * 1000)
            if remaining_ms > 5 and self._timer:
                self._timer.start_once(
                    self.k_settling_timer_id, remaining_ms, self.on_activation_settled
                )
                return

        if (
            not self._target_detector
            or self._target_detector.get_foreground_window() != self._target_handle
            or not self._target_detector.is_editable_target(
                self._target_handle, self._stealthdesk_handle
            )
        ):
            self._state = TyperState.ARMED
            self._publish("Target lost after settling - Armed")
            return

        self._state = TyperState.TYPING
        peek = self._session.peek_codepoint() or 0
        delay_ms = self._timing_model.next_delay_ms(peek, 0, self._speed)
        if self._timer:
            self._timer.start_periodic(self.k_timer_id, delay_ms, self.on_tick)
        if not self._resuming_quietly:
            # Report the real position. A hardcoded "#1" made every resume look
            # like a fresh start, which sent the user hunting for a session
            # reset that was not happening.
            self._publish(
                "Activation settled - typing resumed at char %d"
                % (self._session.position + 1)
            )
        self._resuming_quietly = False

    def calculate_leading_indent_on_next_line(self) -> int:
        """How many whitespace codepoints follow the next newline."""
        cps = self._session.codepoints
        pos = self._session.position + 1  # position after \n
        indent_count = 0
        while pos < len(cps):
            cp = cps[pos]
            if cp == ord(" ") or cp == ord("\t"):
                indent_count += 1
                pos += 1
            else:
                break
        return indent_count

    # -- typing tick -------------------------------------------------------
    def _focus_is_uncertain(self) -> bool:
        """True while a focus change is being decided.

        Keystrokes must not be sent during this window: the foreground is not
        the target, so they would land in whichever window currently has focus
        (typically the console). Waiting is always safe; typing is not.
        """
        if self._focus_lost_since is None or not self._target_handle:
            return False
        if self._state != TyperState.TYPING:
            return False
        return (time.monotonic() - self._focus_lost_since) < FOCUS_LOSS_GRACE_SECONDS

    def on_tick(self) -> None:
        if self._state != TyperState.TYPING:
            return

        self.evaluate_target_window()
        if self._state != TyperState.TYPING:
            return

        # Focus is on its way out and has not yet been judged: hold this tick.
        if self._focus_is_uncertain():
            return

        if self._session.is_complete():
            self.pause_auto()
            self._stop_monitor_timer()
            self._state = TyperState.IDLE
            self._session.clear()
            self._publish("Typing complete")
            return

        # Smart IDE auto-indent compensation: skip leading whitespace on the
        # new line, because the IDE already inserted it.
        if (
            self._pending_indent_to_skip > 0
            and self.effective_auto_indent_mode == AutoIndentMode.SMART_IDE
        ):
            cp_opt = self._session.peek_codepoint()
            if cp_opt is not None and cp_opt in (ord(" "), ord("\t")):
                self._session.consume()
                self._pending_indent_to_skip -= 1
                if self._timer:
                    self._timer.start_once(self.k_timer_id, 1, self.on_tick)
                return
            self._pending_indent_to_skip = 0

        ch_opt = self._session.peek_codepoint()
        if ch_opt is None:
            return

        ch = ch_opt
        ok = self.send_codepoint(ch)

        if not ok:
            # A refusal from the port is a different failure from a delivery
            # failure, and the two need different fixes. Reporting the generic
            # message below would send the user hunting UIPI and elevated-target
            # problems they do not have when the real answer is "no driver".
            why = self.input_refusal()
            if why:
                self.pause_auto()
                self._stop_monitor_timer()
                self._state = TyperState.IDLE
                self._session.clear()
                self._publish(why)
                return

        if ok:
            if ch == ord("\n") and self.effective_auto_indent_mode == AutoIndentMode.SMART_IDE:
                self._pending_indent_to_skip = self.calculate_leading_indent_on_next_line()

            self._session.consume()
            if not self._session.is_complete() and self._timer:
                next_cp = self._session.peek_codepoint() or 0
                delay_ms = self._timing_model.next_delay_ms(ch, next_cp, self._speed)
                self._timer.start_periodic(self.k_timer_id, delay_ms, self.on_tick)
        else:
            self.pause_auto()
            err = self._input.get_last_error() if self._input else 0
            if err == ERROR_NOT_SUPPORTED:
                message = "Cannot type U+%04X - unmapped in active layout" % ch
            elif err == ERROR_ACCESS_DENIED:
                message = "Input delivery failed (UIPI / Elevated target - Run as Admin)"
            elif err != 0:
                message = "Input delivery failed (Error code: %d)" % err
            else:
                message = "Input delivery failed"
            self._publish(message)

    def send_codepoint(self, ch: int) -> bool:
        if not self._input:
            return False

        if ch == ord("\n"):
            down_ok = self._input.inject_virtual_key(VK_RETURN, False)
            up_ok = self._input.inject_virtual_key(VK_RETURN, True)
            return down_ok and up_ok
        if ch == ord("\t"):
            down_ok = self._input.inject_virtual_key(VK_TAB, False)
            up_ok = self._input.inject_virtual_key(VK_TAB, True)
            return down_ok and up_ok
        return self._input.inject_unicode_char(ch)




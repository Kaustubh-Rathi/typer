"""FULL PROOF: focus Notepad, then run the real typer and read back the text."""

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

from stealth_typer.adapters.event_bus import ConsoleEventBus
from stealth_typer.adapters.thread_timer import ThreadTimer
from stealth_typer.adapters.win_input_injection_adapter import WinInputInjectionAdapter
from stealth_typer.adapters.win_target_detector import WinTargetDetector
from stealth_typer.application.typer_service import TyperService
from stealth_typer.core.timing_model import HumanizationLevel
from stealth_typer.core.typing_policy import AutoIndentMode
from stealth_typer.domain.typer_state import TyperState

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
SW_SHOW = 5
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_SETTEXT = 0x000C
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def focused_control(hwnd):
    """The control inside `hwnd` that would actually receive a keystroke.

    Asked of Windows rather than guessed from the class list. Notepad keeps one
    rich edit per tab, so "the first RichEditD2DPT I find" can be a background
    tab that still answers WM_GETTEXT with stale text - which is precisely how
    a test can report success while the visible document is empty.
    """
    pid = wintypes.DWORD(0)
    thread = user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not user32.GetGUIThreadInfo(thread, ctypes.byref(info)):
        return None
    # HWND fields default to a c_void_p that reads back as None when empty.
    return int(info.hwndFocus or 0) or None


def screenshot(path):
    """Save the screen to a BMP.

    The read-back above can be satisfied by a control that is present but not
    the one on screen. A picture is the only evidence that settles it.
    """
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    src = gdi32.CreateDCW("DISPLAY", None, None, None)
    mem = gdi32.CreateCompatibleDC(src)
    bmp = gdi32.CreateCompatibleBitmap(src, w, h)
    gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, w, h, src, 0, 0, SRCCOPY)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    hdr = BITMAPINFOHEADER()
    hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    hdr.biWidth = w
    hdr.biHeight = -h  # negative = top-down
    hdr.biPlanes = 1
    hdr.biBitCount = 32
    hdr.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(hdr), DIB_RGB_COLORS)

    fh = ctypes.create_string_buffer(14 + 40 + len(buf))
    ctypes.memmove(fh, b"BM", 2)
    ctypes.memmove(ctypes.byref(fh, 14), ctypes.byref(hdr), 40)
    ctypes.memmove(ctypes.byref(fh, 14 + 40), buf, len(buf))
    with open(path, "wb") as fh_out:
        fh_out.write(fh.raw)
    print("screenshot ->", path, "(%dx%d)" % (w, h))

TEXT = "HelloLive123 test of single key flow"

# Realistic defaults, matching the command the user actually runs. Typing at
# speed 100 with no humanization is far faster than Notepad's D2D rich edit
# can absorb: it drops and repeats characters (the 'wwwwwwww' tail), which
# looks exactly like a broken injector but is only a rate problem.
SPEED = 70
HUMANIZATION = "medium"


user32.FindWindowExW.restype = wintypes.HWND
user32.SendMessageW.restype = ctypes.c_ulonglong

EDIT_CLASSES = (
    "RichEditD2DPT",
    "RichEditD2D",
    "RichEdit20W",
    "RichEdit20A",
    "Edit",
)


def descendants(hwnd, depth=0, out=None):
    """Every child window, recursively.

    Notepad nests its text view below the top level (inside the XAML island
    tree), so a single FindWindowExW on the frame finds nothing. Walking the
    whole tree is what actually locates the document control.
    """
    if out is None:
        out = []
    if depth > 8:
        return out
    child = user32.GetWindow(hwnd, 5)  # GW_CHILD
    while child:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, buf, 256)
        out.append((int(child), buf.value))
        descendants(int(child), depth + 1, out)
        child = user32.GetWindow(child, 2)  # GW_HWNDNEXT
    return out


def content(hwnd):
    """Read the control that holds keyboard focus.

    Two failure modes are closed off deliberately:
      * the window title is never used, because Notepad mirrors the first line
        there, so a title read reports success on an empty document;
      * the control is not chosen by class order, because Notepad keeps one
        rich edit per tab and a background tab still answers WM_GETTEXT.
    """
    focus = focused_control(hwnd)
    if focus:
        n = user32.SendMessageW(wintypes.HWND(focus), WM_GETTEXTLENGTH, 0, 0)
        buf = ctypes.create_unicode_buffer(int(n) + 2)
        user32.SendMessageW(wintypes.HWND(focus), WM_GETTEXT, n + 2, buf)
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(wintypes.HWND(focus), name, 256)
        return buf.value, name.value

    title = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(title + 2)
    user32.GetWindowTextW(hwnd, buf, title + 2)
    raise AssertionError(
        "no control holds keyboard focus in Notepad (classes seen: %s); "
        "title was %r. Refusing to report MATCH from the title."
        % (sorted({c for _, c in descendants(hwnd)}), buf.value)
    )


def clear_document(hwnd):
    """Empty the focused control, so any later text is unambiguously ours."""
    focus = focused_control(hwnd)
    if focus:
        user32.SendMessageW(wintypes.HWND(focus), WM_SETTEXT, 0, "")
    return bool(focus)


def focus(hwnd):
    fg = user32.GetForegroundWindow()
    mine = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else None
    attached = bool(fg_thread) and bool(user32.AttachThreadInput(mine, fg_thread, True))
    user32.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(HWND_TOPMOST),
                        0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
    user32.ShowWindow(wintypes.HWND(hwnd), SW_SHOW)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.SetActiveWindow(hwnd)
    user32.SetFocus(hwnd)
    if attached:
        user32.AttachThreadInput(mine, fg_thread, False)
    user32.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(HWND_NOTOPMOST),
                        0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
    time.sleep(0.5)


user32.FindWindowExW.restype = wintypes.HWND
user32.SendMessageW.restype = ctypes.c_ulonglong

user32.GetWindow.restype = wintypes.HWND
user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)

print("Opening Notepad...")
proc = subprocess.Popen(["notepad.exe"])
time.sleep(2.5)


def notepad_for_pid(pid, timeout=8.0):
    """The Notepad window belonging to THIS process.

    FindWindowW("Notepad", None) returns whichever Notepad window comes first
    in the z-order, which on a machine that already has one open is a stale
    window: the run then focuses and reads a document nobody is looking at. So
    match on the owning process where possible and fall back to the newest
    visible window, because Notepad can hand its window to a different process.
    """
    deadline = time.monotonic() + timeout
    fallback = 0
    while time.monotonic() < deadline:
        found = []
        visible = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(h, _):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, buf, 256)
            if buf.value == "Notepad":
                owner = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(h, ctypes.byref(owner))
                if not user32.IsWindowVisible(h):
                    return True
                visible.append(int(h))
                if owner.value == pid:
                    found.append(int(h))
            return True

        user32.EnumWindows(visit, 0)
        if found:
            return found[0]
        if visible:
            # Remember the latest one seen; EnumWindows walks in z-order, so the
            # final entry is the most recently opened window.
            fallback = visible[-1]
        time.sleep(0.5)
    return fallback


hwnd = notepad_for_pid(proc.pid)
print("notepad pid:", proc.pid, "hwnd:", hwnd)
if not hwnd:
    raise SystemExit("could not find the Notepad window for pid %d" % proc.pid)
print("child classes:", sorted({c for _, c in descendants(hwnd)}))

detector = WinTargetDetector()
focus(hwnd)
print("foreground:", detector.get_foreground_window(), "== notepad:", detector.get_foreground_window() == hwnd)
print("focused control:", focused_control(hwnd))
print("cleared:", clear_document(hwnd))
print("before:", repr(content(hwnd)[0]))

events = ConsoleEventBus(echo=True)
timer = ThreadTimer()
typer = TyperService(None, WinInputInjectionAdapter(), timer, events, detector)
typer.session().set_text(TEXT)
typer.set_speed(SPEED)
typer.set_humanization_level(HumanizationLevel(HUMANIZATION))
typer.set_activation_settling_delay_ms(0)
typer.set_auto_indent_mode(AutoIndentMode.RAW)

print()
print("--- start, exactly as the single hotkey does ---")
typer.arm(auto_pick=False)
typer.start_monitoring()
typer.start_auto()

deadline = time.monotonic() + 25
while typer.state != TyperState.IDLE and time.monotonic() < deadline:
    time.sleep(0.05)

focus(hwnd)
time.sleep(0.5)
got, cls = content(hwnd)
print()
print("=" * 60)
print("control         :", cls)
print("Notepad content :", repr(got))
print("expected        :", repr(TEXT))
print("MATCH           :", TEXT in got)
print("statuses printed:", len(events.statuses))
print("=" * 60)
screenshot(r"D:\typer\live_proof.bmp")

timer.shutdown()
typer.reset()
proc.terminate()
sys.exit(0 if TEXT in got else 1)

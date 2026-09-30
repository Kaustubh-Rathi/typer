"""Win32 foreground-window detection, ported from
``src/adapters/target/win_target_detector.cpp``.

A window counts as editable when it is valid, visible, enabled, not one of our
own windows, not the desktop/shell, and its thread is not in a menu.
"""

import ctypes
from ctypes import wintypes
from typing import Optional

from ..ports.i_target_detector import ITargetDetector

GA_ROOT = 2
GUI_INMENUMODE = 0x00000004

_NON_EDITABLE_CLASSES = frozenset(
    [
        "#32768",
        "#32769",
        "Progman",
        "WorkerW",
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "MultitaskingViewFrame",
        "SideBar_PropertyHost",
        "DV2ControlHost",
        "Button",
        "STATIC",
        "ToolBarWindow32",
        "ReBarWindow32",
        "msctls_statusbar32",
        "msctls_trackbar32",
        "SysHeader32",
    ]
)


# Windows windows that are foreground only transiently and never actually
# receive the keystrokes we inject.
#
# "ForegroundStaging" is the important one: the shell shows it for a few
# milliseconds during any window activation (including the activation the
# typer's own resume causes). It is not a real focus change, and treating it as
# one produced an endless pause/resume loop in which no character was ever
# delivered, because on_tick re-checks the target before typing.
_TRANSIENT_CLASSES = frozenset(
    [
        "ForegroundStaging",
        "IME",
        "MSCTFIME UI",
        "Default IME",
        "Windows.UI.Core.CoreWindow",
        "XamlExplorerHostIslandWindow",
        "tooltips_class32",
        "NotifyIconOverflowWindow",
        "TaskListThumbnailWnd",
        "TaskListGestureButtonClass",
        "MouseCursor",
        "Caret",
        "Shell_Dialog",
        "Credential Dialog Xaml Host",
        "Xaml_WindowedPopupClass",
        "SearchHostWindow",
        "SearchBox",
    ]
)

_TRANSIENT_TITLES = frozenset(["", "Default IME", "IME"])


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


def _is_non_editable_class(class_name: str) -> bool:
    return class_name in _NON_EDITABLE_CLASSES


# Console and terminal windows, kept separate from the non-editable list
# because they get their own question ("is this OUR console?") in
# WinTargetDetector.is_own_console, and the auto-indent table below reuses the
# same membership test.
_CONSOLE_CLASSES = frozenset(
    [
        "ConsoleWindowClass",
        "PseudoConsoleWindow",
        "CASCADIA_HOSTING_WINDOW_CLASS",
        "TERM",
        "Windows Terminal",
    ]
)


def _is_console_class(class_name: str) -> bool:
    return class_name in _CONSOLE_CLASSES


def _is_transient_class(class_name: str) -> bool:
    return class_name in _TRANSIENT_CLASSES


# Editors that insert their own indentation when you press ENTER. For these,
# replaying the source's leading whitespace would double it.
_AUTO_INDENT_CLASSES = {
    # Notepad++ (Scintilla) auto-indents by default
    "Notepad++": True,
    "Scintilla": True,
    # Word/Excel style rich edit controls do not auto-indent
    "RichEdit20W": False,
    "RichEdit50W": False,
    "RICHEDIT50W": False,
    # Plain Notepad does not auto-indent
    "Notepad": False,
    # Terminals and consoles do not
    "ConsoleWindowClass": False,
    "CASCADIA_HOSTING_WINDOW_CLASS": False,
}

# Same question, asked of the process image name when the window class is
# generic or shared (VS Code, JetBrains IDEs and Electron apps all reuse
# Chrome_WidgetWin_1). Keys are lowercase image names.
_AUTO_INDENT_PROCESSES = {
    # Auto-indenting editors
    "code.exe": True,  # VS Code
    "pycharm64.exe": True,
    "pycharm.exe": True,
    "idea64.exe": True,  # IntelliJ
    "idea.exe": True,
    "webstorm64.exe": True,
    "notepad++.exe": True,
    "devenv.exe": True,  # Visual Studio
    "sublime_text.exe": True,
    "cursor.exe": True,
    "zed.exe": True,
    " brackets.exe": True,
    # Plain editors that do NOT auto-indent
    "notepad.exe": False,
    "wordpad.exe": False,
    "winword.exe": False,
    "excel.exe": False,
    "cmd.exe": False,
    "powershell.exe": False,
    "pwsh.exe": False,
    "windowsterminal.exe": False,
}


class WinTargetDetector(ITargetDetector):
    def __init__(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # The console this process is attached to. Never a valid target: it is
        # where the typer's own output goes, so typing into it means typing
        # over the status messages. Cached because it cannot change mid-run.
        self._own_console = int(self._kernel32.GetConsoleWindow() or 0)
        self._own_pid = int(self._kernel32.GetCurrentProcessId())
        # The root ancestor of that console. In Windows Terminal, conhost and
        # VS Code the console window is a child of the real top-level window,
        # so the class check alone is not enough to recognise it.
        self._own_console_root = (
            int(self._user32.GetAncestor(wintypes.HWND(self._own_console), GA_ROOT) or 0)
            if self._own_console
            else 0
        )

    def is_own_console(self, handle: int) -> bool:
        """Whether ``handle`` is this process's console window or its host.

        Three checks, because no single one is sufficient:

        1. ``GetConsoleWindow`` - the classic conhost console. Returns 0 under
           a pseudo-console (ConPTY), which is what Windows Terminal, VS Code
           and WezTerm use, so this alone is not enough.
        2. Window class - catches the pseudo-console hosts by name.
        3. Owning process - if the window belongs to this very process, it is
           our console by definition, whatever it is called.
        """
        if not handle:
            return False
        if self._own_console and handle == self._own_console:
            return True
        if _is_console_class(self._class_name(handle)):
            return True
        if self._own_console_root:
            if handle == self._own_console_root:
                return True
            # The console may be any descendant of the host window.
            if self._user32.IsChild(
                wintypes.HWND(self._own_console_root), wintypes.HWND(handle)
            ):
                return True
        return False

    def _owning_pid(self, handle: int) -> int:
        pid = wintypes.DWORD(0)
        self._user32.GetWindowThreadProcessId(wintypes.HWND(handle), ctypes.byref(pid))
        return int(pid.value)

    def _class_name(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(wintypes.HWND(hwnd), buffer, 256)
        return buffer.value

    def is_stealthdesk_window(self, handle: int, stealthdesk_handle: int) -> bool:
        if not handle or not stealthdesk_handle:
            return False
        if handle == stealthdesk_handle:
            return True
        if self._user32.IsChild(wintypes.HWND(stealthdesk_handle), wintypes.HWND(handle)):
            return True
        root = self._user32.GetAncestor(wintypes.HWND(handle), GA_ROOT)
        return int(root or 0) == stealthdesk_handle

    def is_window_valid(self, handle: int) -> bool:
        if not handle:
            return False
        return bool(self._user32.IsWindow(wintypes.HWND(handle)))

    def is_transient_overlay(self, handle: int) -> bool:
        """Whether ``handle`` is a shell/IME/tooltip window that must be ignored.

        These appear as foreground for a few milliseconds without ever
        receiving injected keystrokes. Treating them as a focus change makes
        typing thrash: pause, resume, pause again. Required for ForwardStaging
        in particular, which the shell flashes during ordinary window
        activation.
        """
        if not handle:
            return False
        try:
            if _is_transient_class(self._class_name(handle)):
                return True
            length = self._user32.GetWindowTextLengthW(wintypes.HWND(handle))
            if length == 0:
                # An untitled, invisible window is not a typing destination.
                return not self._user32.IsWindowVisible(wintypes.HWND(handle))
            buffer = ctypes.create_unicode_buffer(length + 1)
            self._user32.GetWindowTextW(wintypes.HWND(handle), buffer, length + 1)
            return buffer.value in _TRANSIENT_TITLES
        except Exception:
            return False

    def get_foreground_window(self) -> int:
        return int(self._user32.GetForegroundWindow() or 0)

    def has_keyboard_focus(self, handle: int) -> bool:
        """Whether ``handle`` (or a window inside it) currently has focus.

        Separated from :meth:`is_editable_target` on purpose. A visible,
        enabled Notepad in the background is perfectly editable, but typing into
        it would go to whichever window really has the caret - so the arm step
        must require focus as well as editability.
        """
        if not handle:
            return False
        foreground = self.get_foreground_window()
        if not foreground:
            return False
        if foreground == handle:
            return True
        # The foreground may be a child (an EDIT control) of the target window.
        if self._user32.IsChild(
            wintypes.HWND(handle), wintypes.HWND(foreground)
        ):
            return True
        # Or the target may be a child of the foreground (a dialog's edit box).
        root = self._user32.GetAncestor(wintypes.HWND(handle), GA_ROOT)
        return bool(root) and int(root) == foreground

    def is_editable_target(self, target_handle: int, stealthdesk_handle: int) -> bool:
        """Whether this handle is a usable typing destination.

        Note this does NOT check that the window is *focused*. Callers pass
        whichever handle they are considering - normally the foreground
        window, but possibly a remembered target. Focus is checked separately
        by :meth:`has_keyboard_focus`, so this stays a pure "is this window
        capable of receiving text" test.
        """
        if not target_handle:
            return False
        hwnd = wintypes.HWND(target_handle)
        if not self._user32.IsWindow(hwnd) or not self._user32.IsWindowVisible(hwnd):
            return False

        if self.is_stealthdesk_window(target_handle, stealthdesk_handle):
            return False

        if _is_console_class(self._class_name(target_handle)):
            return False

        # Never type into the console the typer itself is running in.
        if self.is_own_console(target_handle):
            return False

        # Finally, a window owned by this very process is our own output
        # surface by definition, whatever it happens to be called.
        if self._owning_pid(target_handle) == self._own_pid:
            return False

        # Desktop / shell windows are never editable.
        if int(self._user32.GetDesktopWindow() or 0) == target_handle:
            return False
        if _is_non_editable_class(self._class_name(target_handle)):
            return False

        thread_id = self._user32.GetWindowThreadProcessId(hwnd, None)
        if thread_id:
            info = GUITHREADINFO()
            info.cbSize = ctypes.sizeof(GUITHREADINFO)
            if self._user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
                if info.flags & GUI_INMENUMODE:
                    return False
                if info.hwndFocus and _is_non_editable_class(
                    self._class_name(int(info.hwndFocus))
                ):
                    return False

        if not self._user32.IsWindowEnabled(hwnd):
            return False

        return True

    def is_auto_indenting_editor(self, handle: int) -> Optional[bool]:
        """Decide whether the target inserts its own indentation after ENTER.

        Checked against the window class first (cheap, no extra syscalls), then
        the owning process image name, which is what actually identifies
        Notepad/VS Code/PyCharm when several share a generic class.

        Returns ``None`` when the editor is not recognised, so the caller can
        apply its own fallback instead of guessing here.
        """
        if not handle or not self.is_window_valid(handle):
            return None

        class_name = self._class_name(handle)
        decision = _AUTO_INDENT_CLASSES.get(class_name)
        if decision is not None:
            return decision

        exe = self._process_image_name(handle)
        if exe:
            return _AUTO_INDENT_PROCESSES.get(exe)
        return None

    def _process_image_name(self, handle: int) -> str:
        """Lowercase process image name (e.g. 'code.exe') owning the window."""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        pid = wintypes.DWORD(0)
        self._user32.GetWindowThreadProcessId(
            wintypes.HWND(handle), ctypes.byref(pid)
        )
        if not pid.value:
            return ""

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        opened = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not opened:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                opened, 0, buffer, ctypes.byref(size)
            ):
                return ""
            path = buffer.value
            return path.rsplit("\\", 1)[-1].lower()
        finally:
            kernel32.CloseHandle(opened)

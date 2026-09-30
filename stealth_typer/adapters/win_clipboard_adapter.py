"""Windows clipboard text adapter (``CF_UNICODETEXT``) via ``ctypes``."""

import ctypes
from ctypes import wintypes

from ..ports.i_clipboard_port import IClipboardPort

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


class WinClipboardAdapter(IClipboardPort):
    def __init__(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self._user32.OpenClipboard.argtypes = [wintypes.HWND]
        self._user32.OpenClipboard.restype = wintypes.BOOL
        self._user32.CloseClipboard.argtypes = []
        self._user32.CloseClipboard.restype = wintypes.BOOL
        self._user32.EmptyClipboard.argtypes = []
        self._user32.EmptyClipboard.restype = wintypes.BOOL
        self._user32.GetClipboardData.argtypes = [wintypes.UINT]
        self._user32.GetClipboardData.restype = wintypes.HANDLE
        self._user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        self._user32.SetClipboardData.restype = wintypes.HANDLE

        self._kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self._kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        self._kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        self._kernel32.GlobalLock.restype = wintypes.LPVOID
        self._kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        self._kernel32.GlobalUnlock.restype = wintypes.BOOL

    def get_text(self) -> str:
        if not self._user32.OpenClipboard(None):
            return ""
        try:
            handle = self._user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = self._kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            try:
                return ctypes.wstring_at(pointer)
            finally:
                self._kernel32.GlobalUnlock(handle)
        finally:
            self._user32.CloseClipboard()

    def set_text(self, text: str) -> None:
        if not self._user32.OpenClipboard(None):
            return
        try:
            self._user32.EmptyClipboard()
            size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
            handle = self._kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                return
            pointer = self._kernel32.GlobalLock(handle)
            if not pointer:
                return
            try:
                ctypes.memmove(pointer, ctypes.c_wchar_p(text), size)
            finally:
                self._kernel32.GlobalUnlock(handle)
            self._user32.SetClipboardData(CF_UNICODETEXT, handle)
        finally:
            self._user32.CloseClipboard()

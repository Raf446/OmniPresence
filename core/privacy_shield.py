"""Win32 Anti-AFK Privacy Shield & Biometric Lock Hook (wtsapi32.dll + GetLastInputInfo)."""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes

from PySide6.QtCore import QThread, Signal

WM_WTSSESSION_CHANGE = 0x02B1
WM_DESTROY = 0x0002
WM_QUIT = 0x0012
PM_REMOVE = 0x0001

WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


if sys.platform == "win32":
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    wtsapi32 = ctypes.windll.wtsapi32

    WNDPROCTYPE = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    class _WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROCTYPE),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HANDLE),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HANDLE),
        ]

    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t

    wtsapi32.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
    wtsapi32.WTSRegisterSessionNotification.restype = wintypes.BOOL
    wtsapi32.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
    wtsapi32.WTSUnRegisterSessionNotification.restype = wintypes.BOOL
    kernel32.GetTickCount.restype = wintypes.DWORD


def query_idle_seconds() -> float:
    """Return OS input idle duration in seconds via Win32 GetLastInputInfo()."""
    if sys.platform != "win32":
        return 0.0
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    elapsed_ms = (kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
    return elapsed_ms / 1000.0


class PrivacyShield(QThread):
    """Monitors Win32 workstation lock/unlock events (Win+L) and inactivity threshold."""

    shield_engaged = Signal(bool)
    shield_reason = Signal(bool, str)

    def __init__(
        self,
        enabled: bool = True,
        lock_hook: bool = True,
        inactivity_minutes: int = 10,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._enabled = bool(enabled)
        self._lock_hook = bool(lock_hook)
        self._inactivity_minutes = int(inactivity_minutes)
        self._session_locked = False
        self._idle_locked = False
        self._engaged = False
        self._halt = threading.Event()
        self._hwnd: int | None = None

    @property
    def is_engaged(self) -> bool:
        with self._lock:
            return self._engaged

    def configure(
        self,
        *,
        enabled: bool | None = None,
        lock_hook: bool | None = None,
        inactivity_minutes: int | None = None,
    ) -> None:
        """Update Privacy Shield parameters at runtime without restarting."""
        with self._lock:
            if enabled is not None:
                self._enabled = bool(enabled)
            if lock_hook is not None:
                self._lock_hook = bool(lock_hook)
            if inactivity_minutes is not None:
                self._inactivity_minutes = max(0, int(inactivity_minutes))
        self._evaluate_state()

    def simulate_session_lock(self, locked: bool) -> None:
        """Trigger or release workstation lock state (used by Win32 WndProc and unit tests)."""
        with self._lock:
            self._session_locked = bool(locked)
        self._evaluate_state(reason="workstation_lock" if locked else "workstation_unlock")

    def check_inactivity(self, idle_seconds: float | None = None) -> None:
        """Evaluate inactivity threshold using GetLastInputInfo() or an injected idle duration."""
        current_idle = query_idle_seconds() if idle_seconds is None else float(idle_seconds)
        with self._lock:
            threshold_sec = self._inactivity_minutes * 60.0 if self._inactivity_minutes > 0 else 0.0
            should_idle_lock = bool(self._enabled and threshold_sec > 0 and current_idle >= threshold_sec)
            if should_idle_lock == self._idle_locked:
                return
            self._idle_locked = should_idle_lock
        self._evaluate_state(reason="inactivity_timeout" if should_idle_lock else "user_input_resumed")

    def _evaluate_state(self, reason: str = "config_update") -> None:
        with self._lock:
            should_engage = bool(
                self._enabled
                and ((self._lock_hook and self._session_locked) or self._idle_locked)
            )
            if should_engage == self._engaged:
                return
            self._engaged = should_engage

        self.shield_engaged.emit(should_engage)
        self.shield_reason.emit(should_engage, reason)

    def stop(self) -> None:
        self._halt.set()
        self.wait(3000)

    def run(self) -> None:
        if sys.platform != "win32":
            while not self._halt.is_set():
                time.sleep(0.5)
            return

        hwnd = self._create_session_window()
        self._hwnd = hwnd
        registered = False
        if hwnd:
            registered = bool(
                wtsapi32.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION)
            )

        msg = wintypes.MSG()
        try:
            while not self._halt.is_set():
                if hwnd:
                    while user32.PeekMessageW(ctypes.byref(msg), hwnd, 0, 0, PM_REMOVE):
                        user32.TranslateMessage(ctypes.byref(msg))
                        user32.DispatchMessageW(ctypes.byref(msg))
                self.check_inactivity()
                self._halt.wait(0.4)
        finally:
            if hwnd and registered:
                try:
                    wtsapi32.WTSUnRegisterSessionNotification(hwnd)
                except Exception:
                    pass
            if hwnd:
                try:
                    user32.DestroyWindow(hwnd)
                except Exception:
                    pass
            self._hwnd = None

    def _create_session_window(self) -> int | None:
        if sys.platform != "win32":
            return None

        def _wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_WTSSESSION_CHANGE:
                if wparam == WTS_SESSION_LOCK:
                    self.simulate_session_lock(True)
                elif wparam == WTS_SESSION_UNLOCK:
                    self.simulate_session_lock(False)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wnd_proc_ref = WNDPROCTYPE(_wnd_proc)
        hinst = kernel32.GetModuleHandleW(None)
        class_name = f"OmniPresencePrivacyShield_{id(self)}"

        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = self._wnd_proc_ref
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinst
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = class_name
        wc.hIconSm = None

        user32.RegisterClassExW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            "OmniPresenceSessionWatcher",
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            hinst,
            None,
        )
        return int(hwnd) if hwnd else None


PRIVACY_SHIELD = PrivacyShield()

"""Foreground window and process polling, woken early by a WinEvent hook."""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import psutil
from PySide6.QtCore import QThread, Signal

from core.priority_engine import Activity, RuleBook, Rule, WindowFacts, render
from extractors.compiler_listener import BuildInfo, CompilerListener
from extractors.git_extractor import GitExtractor
from extractors.telemetry import TelemetrySample, TelemetrySampler

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_NAMECHANGE = 0x800C
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
OBJID_WINDOW = 0
CHILDID_SELF = 0
WM_QUIT = 0x0012
GWL_STYLE = -16
WS_CAPTION = 0x00C00000
MONITOR_DEFAULTTONEAREST = 2
SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})

WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD
)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.GetLastInputInfo.restype = wintypes.BOOL
user32.SetWinEventHook.argtypes = [
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE, WINEVENTPROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
]
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
user32.UnhookWinEvent.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
kernel32.GetTickCount.restype = wintypes.DWORD
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


@dataclass(frozen=True)
class ProcRecord:
    pid: int
    ppid: int
    name: str
    create_time: float


class ProcessTable:
    """Incremental process list: only pids that appeared since the last tick are inspected."""

    def __init__(self) -> None:
        self._records: dict[int, ProcRecord] = {}

    def refresh(self) -> None:
        live = set(psutil.pids())
        for pid in self._records.keys() - live:
            del self._records[pid]
        for pid in live - self._records.keys():
            self._records[pid] = self._load(pid)

    def records(self) -> list[ProcRecord]:
        return list(self._records.values())

    def names(self) -> frozenset[str]:
        return frozenset(r.name for r in self._records.values() if r.name)

    def lookup(self, pid: int) -> ProcRecord:
        record = self._records.get(pid)
        if record is None:
            record = self._records[pid] = self._load(pid)
        return record

    @staticmethod
    def _load(pid: int) -> ProcRecord:
        try:
            process = psutil.Process(pid)
            with process.oneshot():
                return ProcRecord(pid, process.ppid(), process.name().lower(), process.create_time())
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return ProcRecord(pid, 0, "", 0.0)


class WindowProbe:
    """Thin ctypes wrapper over the user32 calls needed to describe the foreground window."""

    @staticmethod
    def foreground(table: ProcessTable) -> WindowFacts | None:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value
        record = table.lookup(pid.value)
        return WindowFacts(
            pid=pid.value,
            process=record.name,
            title=title,
            fullscreen=WindowProbe._is_fullscreen(hwnd),
            started_at=record.create_time or None,
        )

    @staticmethod
    def _is_fullscreen(hwnd) -> bool:
        class_name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, class_name, 64)
        if class_name.value in SHELL_CLASSES:
            return False
        # Captioned windows are ordinary maximised apps even when the taskbar is hidden.
        if user32.GetWindowLongW(hwnd, GWL_STYLE) & WS_CAPTION == WS_CAPTION:
            return False
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return False
        area = info.rcMonitor
        return (
            rect.left <= area.left and rect.top <= area.top and rect.right >= area.right and rect.bottom >= area.bottom
        )

    @staticmethod
    def idle_seconds() -> float:
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        elapsed_ms = (kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        return elapsed_ms / 1000.0


class ForegroundHook:
    """Calls `on_event` whenever the foreground window changes or retitles itself."""

    def __init__(self, on_event: Callable[[], None]) -> None:
        self._on_event = on_event
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._pump, name="omni-winevent", daemon=True)
        self._thread.start()
        self._ready.wait(2.0)

    def stop(self) -> None:
        if self._thread is None:
            return
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(2.0)

    def _callback(self, _hook, event, hwnd, id_object, id_child, _thread, _time) -> None:
        if event == EVENT_OBJECT_NAMECHANGE:
            if id_object != OBJID_WINDOW or id_child != CHILDID_SELF or hwnd != user32.GetForegroundWindow():
                return
        self._on_event()

    def _pump(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        proc = WINEVENTPROC(self._callback)
        flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
        hooks = [
            user32.SetWinEventHook(event, event, None, proc, 0, 0, flags)
            for event in (EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_NAMECHANGE)
        ]
        self._ready.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        for hook in hooks:
            if hook:
                user32.UnhookWinEvent(hook)


def _protected_roots() -> tuple[Path, ...]:
    roots = []
    for variable in ("WINDIR", "ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(variable):
            roots.append(Path(os.environ[variable]))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Programs")
    return tuple(roots)


_PROTECTED_ROOTS = _protected_roots()
_VSCODE_STORAGE = {"code.exe": "Code", "code - insiders.exe": "Code - Insiders", "vscodium.exe": "VSCodium"}


def _uri_to_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    raw = unquote(parsed.path)
    if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    if parsed.netloc:
        raw = f"//{parsed.netloc}{raw}"
    return Path(raw)


class WorkspaceResolver:
    """Finds the project folder behind an editor window using cheap, read-only sources."""

    MEMO_TTL = 20.0

    def __init__(self) -> None:
        self._storage: dict[Path, tuple[int, list[Path]]] = {}
        self._memo: dict[tuple, tuple[Path | None, float]] = {}

    def resolve(
        self, strategies: tuple[str, ...], window: WindowFacts | None, hint: str, build: BuildInfo | None, now: float
    ) -> Path | None:
        key = (
            strategies,
            window.pid if window else 0,
            hint,
            build.cwd if build else "",
        )
        cached = self._memo.get(key)
        if cached and now - cached[1] < self.MEMO_TTL:
            return cached[0]
        found: Path | None = None
        for strategy in strategies:
            candidate = getattr(self, "_by_" + strategy)(window, hint, build)
            if candidate is not None and self._is_dir(candidate):
                found = candidate
                break
        if len(self._memo) > 64:
            self._memo.clear()
        self._memo[key] = (found, now)
        return found

    @staticmethod
    def _is_dir(path: Path) -> bool:
        try:
            return path.is_dir()
        except OSError:
            return False

    @staticmethod
    def _protected(path: Path) -> bool:
        return any(root == path or root in path.parents for root in _PROTECTED_ROOTS)

    @staticmethod
    def _hint_matches(path: Path, hint: str) -> bool:
        wanted = hint.lower()
        return path.name.lower() == wanted or any(parent.name.lower() == wanted for parent in path.parents)

    def _by_build_cwd(self, window, hint, build) -> Path | None:
        return Path(build.cwd) if build and build.cwd else None

    def _by_vscode_storage(self, window, hint, build) -> Path | None:
        appdata = os.environ.get("APPDATA")
        variant = _VSCODE_STORAGE.get(window.process) if window else None
        if not appdata or not variant or not hint:
            return None
        folders = self._storage_folders(Path(appdata) / variant / "User" / "globalStorage" / "storage.json")
        wanted = hint.lower()
        for folder in folders:
            if folder.name.lower() == wanted or folder.stem.lower() == wanted:
                return folder if folder.suffix != ".code-workspace" else folder.parent
        return None

    def _by_cmdline(self, window, hint, build) -> Path | None:
        if window is None:
            return None
        try:
            process = psutil.Process(window.pid)
            args = process.cmdline()[1:]
            exe_dir = Path(process.exe()).parent
        except (psutil.Error, OSError):
            return None
        for arg in args:
            if arg.startswith("-"):
                continue
            try:
                path = Path(arg)
                if not path.exists():
                    continue
                folder = path if path.is_dir() else path.parent
            except (OSError, ValueError):
                continue
            if folder == exe_dir or exe_dir in folder.parents or self._protected(folder):
                continue
            if window.process in _VSCODE_STORAGE and hint and not self._hint_matches(folder, hint):
                continue
            return folder
        return None

    def _by_cwd(self, window, hint, build) -> Path | None:
        if window is None:
            return None
        try:
            process = psutil.Process(window.pid)
            folder = Path(process.cwd())
            exe_dir = Path(process.exe()).parent
        except (psutil.Error, OSError):
            return None
        if folder == exe_dir or exe_dir in folder.parents or self._protected(folder):
            return None
        if window.process in _VSCODE_STORAGE and hint and not self._hint_matches(folder, hint):
            return None
        return folder

    def _storage_folders(self, path: Path) -> list[Path]:
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return []
        cached = self._storage.get(path)
        if cached and cached[0] == stamp:
            return cached[1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

        uris: list[str] = []
        state = data.get("windowsState") or {}
        for window in [state.get("lastActiveWindow"), *(state.get("openedWindows") or [])]:
            if not isinstance(window, dict):
                continue
            uris.append(window.get("folder") or "")
            identifier = window.get("workspaceIdentifier")
            if isinstance(identifier, dict):
                uris.append(identifier.get("configURIPath") or "")
        backup = data.get("backupWorkspaces") or {}
        uris += [item.get("folderUri", "") for item in backup.get("folders") or [] if isinstance(item, dict)]
        uris += list((data.get("profileAssociations") or {}).get("workspaces") or {})

        folders: list[Path] = []
        for uri in uris:
            folder = _uri_to_path(uri) if uri else None
            if folder is not None and folder not in folders:
                folders.append(folder)
        self._storage[path] = (stamp, folders)
        return folders


@dataclass(frozen=True)
class ScanResult:
    taken_at: float
    activities: tuple[Activity, ...]
    foreground: WindowFacts | None
    idle_seconds: float
    telemetry: TelemetrySample
    build: BuildInfo | None


class ProcessWatcher(QThread):
    """Polls the OS on a worker thread and emits one ScanResult per tick."""

    scanned = Signal(object)
    message = Signal(str, str)

    MIN_CYCLE_SECONDS = 0.04
    FULL_SCAN_INTERVAL = 0.85

    def __init__(self, rulebook: RuleBook, interval: float, build_linger: float, parent=None) -> None:
        super().__init__(parent)
        self._book = rulebook
        self._interval = max(0.25, interval)
        self._halt = threading.Event()
        self._wake = threading.Event()
        self._table = ProcessTable()
        self._compiler = CompilerListener(build_linger)
        self._telemetry = TelemetrySampler()
        self._git = GitExtractor()
        self._resolver = WorkspaceResolver()
        self._sticky: dict[str, tuple[Activity, float, str]] = {}
        self._reset_sticky = False
        self._last_error = ""
        self._last_full_scan = 0.0
        self._last_build: BuildInfo | None = None
        self._last_telemetry: TelemetrySample | None = None

    def on_rules_changed(self, rulebook: RuleBook, _error: str = "") -> None:
        self._book = rulebook
        self._reset_sticky = True
        self._last_full_scan = 0.0
        self._wake.set()

    def set_interval(self, interval: float) -> None:
        self._interval = max(0.25, interval)

    def poke(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()
        self.wait(4000)

    def run(self) -> None:
        hook = ForegroundHook(self._wake.set)
        hook.start()
        try:
            while not self._halt.is_set():
                self._wake.clear()
                t0 = time.monotonic()
                try:
                    self.scanned.emit(self._scan())
                    self._last_error = ""
                except Exception as exc:
                    text = f"scan failed: {exc!r}"
                    if text != self._last_error:
                        self._last_error = text
                        self.message.emit("error", text)
                # Wait for either the polling interval or an instant WinEvent foreground wakeup
                self._wake.wait(self._interval)
                elapsed = time.monotonic() - t0
                if elapsed < self.MIN_CYCLE_SECONDS and not self._halt.is_set():
                    self._halt.wait(self.MIN_CYCLE_SECONDS - elapsed)
        finally:
            hook.stop()

    def _scan(self) -> ScanResult:
        now = time.time()
        book = self._book
        if self._reset_sticky:
            self._sticky.clear()
            self._reset_sticky = False

        # Fast-path on instant WinEvent wakeups: only refresh full PID table / compiler / telemetry every FULL_SCAN_INTERVAL
        need_full = (
            self._last_telemetry is None
            or (now - self._last_full_scan) >= min(self.FULL_SCAN_INTERVAL, self._interval)
        )
        if need_full:
            self._table.refresh()
            self._last_build = self._compiler.poll(self._table.records(), now)
            self._last_telemetry = self._telemetry.sample()
            self._last_full_scan = now

        window = WindowProbe.foreground(self._table)
        idle = WindowProbe.idle_seconds()
        build = self._last_build
        telemetry = self._last_telemetry
        assert telemetry is not None
        shared = telemetry.as_variables()

        activities: list[Activity] = []
        fresh: set[str] = set()
        for rule, groups in book.match_window(window, build is not None):
            activity = self._make_activity(book, rule, groups, window, build, shared, now)
            activities.append(activity)
            fresh.add(rule.id)
            if rule.sticky_seconds > 0 and window is not None:
                self._sticky[rule.id] = (activity, now, window.process)
        activities += self._carry_sticky(book, fresh, now)
        return ScanResult(now, tuple(activities), window, idle, telemetry, build)

    def _carry_sticky(self, book: RuleBook, fresh: set[str], now: float) -> list[Activity]:
        carried: list[Activity] = []
        running = self._table.names()
        for rule_id, (activity, seen, process) in list(self._sticky.items()):
            rule = book.get(rule_id)
            if rule is None or not rule.enabled or rule.sticky_seconds <= 0:
                del self._sticky[rule_id]
            elif rule_id in fresh:
                continue
            elif now - seen > rule.sticky_seconds or process not in running:
                del self._sticky[rule_id]
            else:
                carried.append(
                    Activity(activity.rule_id, activity.key, activity.variables, activity.started_at, None, False)
                )
        return carried

    def _make_activity(
        self,
        book: RuleBook,
        rule: Rule,
        groups: dict[str, str],
        window: WindowFacts | None,
        build: BuildInfo | None,
        shared: dict[str, str],
        now: float,
    ) -> Activity:
        variables = dict(shared)
        if window is not None:
            stem = window.process[:-4] if window.process.endswith(".exe") else window.process
            variables.update(
                process=window.process, process_name=stem, title=window.title, app_label=book.label_for(window.process)
            )
        if rule.kind == "build" and build is not None:
            variables.update(tool=build.tool, target=build.target, project=build.project)
        variables.update({k: v for k, v in groups.items() if v})
        project = variables.get("project", "")
        if project.endswith(" (Workspace)"):
            project = project[: -len(" (Workspace)")]
        variables["project"] = project

        if rule.workspace:
            folder = self._resolver.resolve(rule.workspace, window, project, build, now)
            if folder is not None:
                if not project:
                    variables["project"] = folder.name
                info = self._git.inspect(folder)
                if info is not None:
                    variables.update(
                        branch=info.branch,
                        sync=info.sync_text,
                        sync_arrows=info.sync_arrows,
                        repo_url=info.remote_url,
                        repo=info.repo_slug,
                    )
        filename = variables.get("file", "")
        if filename:
            language = book.language_for(filename)
            if language:
                variables["language"] = language

        key = (render(rule.session_key, variables) if rule.session_key else None) or rule.id
        if rule.kind == "build" and build is not None:
            started = build.started_at
        else:
            started = window.started_at if window is not None else None
        return Activity(rule.id, key, variables, started)

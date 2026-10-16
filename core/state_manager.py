"""Central reactive state: wires workers, the priority engine and the UI together."""
from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from core.priority_engine import (
    EngineInput,
    PresenceData,
    PriorityEngine,
    Resolution,
    RuleBook,
    RuleError,
)

CLIENT_ID_ENV = "OMNIPRESENCE_CLIENT_ID"


def _coerce(value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    if isinstance(default, list):
        return [str(item) for item in value]
    return str(value)


@dataclass
class Settings:
    discord_client_id: str = ""
    discord_app_name: str = "OmniPresence"
    profile_display_name: str = ""
    profile_handle: str = ""
    polling_interval: float = 1.0
    auto_start: bool = False
    tray_minimized: bool = True
    idle_threshold_seconds: int = 300
    show_idle_presence: bool = True
    show_buttons: bool = True
    media_preferred_apps: list[str] = field(default_factory=lambda: ["spotify", "applemusic"])
    build_linger_seconds: float = 4.0
    log_capacity: int = 500
    path: Path | None = field(default=None, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path) -> "Settings":
        settings = cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        for item in fields(cls):
            if item.name == "path" or item.name not in raw:
                continue
            default = item.default if item.default is not MISSING else item.default_factory()
            try:
                setattr(settings, item.name, _coerce(raw[item.name], default))
            except (TypeError, ValueError):
                continue
        settings.polling_interval = min(max(settings.polling_interval, 0.25), 30.0)
        settings.idle_threshold_seconds = min(max(settings.idle_threshold_seconds, 30), 86400)
        settings.log_capacity = min(max(settings.log_capacity, 50), 5000)
        env_id = os.environ.get(CLIENT_ID_ENV, "").strip()
        if env_id and not settings.discord_client_id:
            settings.discord_client_id = env_id
        return settings

    def save(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for item in fields(self):
            if item.name != "path":
                data[item.name] = getattr(self, item.name)
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class LogEntry:
    timestamp: float
    level: str
    source: str
    text: str


class StateManager(QObject):
    presence_changed = Signal(object)
    outgoing_changed = Signal(object)
    connection_changed = Signal(str, str)
    telemetry_changed = Signal(object)
    log_added = Signal(object)
    rules_changed = Signal(object, str)
    paused_changed = Signal(bool)
    manual_changed = Signal(bool)
    client_id_changed = Signal(str)
    profile_changed = Signal(str, str)

    def __init__(
        self, settings: Settings, rules_path: Path, rulebook: RuleBook, rule_error: str = "", parent=None
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self._rules_path = rules_path
        self.rulebook = rulebook
        self.rule_error = rule_error
        self.engine = PriorityEngine(
            rulebook, settings.idle_threshold_seconds, settings.show_idle_presence, settings.show_buttons
        )
        self.log_history: deque[LogEntry] = deque(maxlen=settings.log_capacity)
        self.resolution: Resolution | None = None
        self.connection: tuple[str, str] = ("disconnected", "Starting")
        self.paused = False

        self._ipc_profile: tuple[str, str] = ("", "")
        self._activities: tuple = ()
        self._media = None
        self._manual: tuple[str, str] | None = None
        self._idle_seconds = 0.0
        self._shared: dict[str, str] = {}
        self._last_signature: tuple | None = None
        self._last_outgoing: Any = object()
        self._last_trigger: tuple[str, str] | None = None

    @property
    def profile(self) -> tuple[str, str]:
        """Return (display_name, handle) prioritizing settings.json overrides, then IPC READY user, then neutral fallback."""
        cfg_name = self.settings.profile_display_name.strip()
        cfg_handle = self.settings.profile_handle.strip()
        ipc_name, ipc_handle = self._ipc_profile

        display_name = cfg_name or ipc_name or "Discord User"
        raw_handle = cfg_handle or ipc_handle
        if raw_handle:
            handle = raw_handle if raw_handle.startswith("@") else f"@{raw_handle}"
        elif cfg_name:
            handle = "@" + "_".join(cfg_name.lower().split())
        else:
            handle = "@discord"
        return display_name, handle

    def log(self, level: str, source: str, text: str) -> None:
        entry = LogEntry(time.time(), level, source, text)
        self.log_history.append(entry)
        self.log_added.emit(entry)

    @Slot(object)
    def on_scan(self, result) -> None:
        self._activities = result.activities
        self._idle_seconds = result.idle_seconds
        self._shared = result.telemetry.as_variables()
        self.telemetry_changed.emit(result.telemetry)
        self._reevaluate()

    @Slot(object)
    def on_media(self, info) -> None:
        prev_key = getattr(self._media, "track_key", None) if self._media is not None else None
        new_key = getattr(info, "track_key", None) if info is not None else None
        self._media = info
        if info is None:
            if prev_key is not None:
                self.log("info", "media", "playback stopped")
        elif new_key != prev_key:
            self.log("info", "media", f"now playing: {info.title}" + (f" by {info.artist}" if info.artist else ""))
        self._reevaluate()

    @Slot(str, str)
    def on_watcher_message(self, level: str, text: str) -> None:
        self.log(level, "watcher", text)

    @Slot(str, str)
    def on_media_message(self, level: str, text: str) -> None:
        self.log(level, "media", text)

    @Slot(str, str)
    def on_ipc_message(self, level: str, text: str) -> None:
        self.log(level, "ipc", text)

    @Slot(str, str)
    def on_ipc_user(self, display_name: str, handle: str) -> None:
        updated = (display_name.strip(), handle.strip())
        if updated != self._ipc_profile:
            self._ipc_profile = updated
            self.profile_changed.emit(*self.profile)

    @Slot(str, str)
    def on_connection(self, status: str, detail: str) -> None:
        changed = self.connection[0] != status
        self.connection = (status, detail)
        self.connection_changed.emit(status, detail)
        if changed and status in {"error", "disconnected"}:
            self.log("warn" if status == "disconnected" else "error", "ipc", detail)

    @Slot(object)
    def on_presence_sent(self, presence: PresenceData | None) -> None:
        if presence is None:
            self.log("info", "ipc", "presence cleared")
            return
        parts = [p for p in (presence.details, presence.state) if p]
        self.log("info", "ipc", "presence sent: " + " | ".join(parts))

    def set_manual(self, enabled: bool, details: str, state: str) -> bool:
        details, state = details.strip(), state.strip()
        if enabled and len(details) < 2:
            self.log("warn", "manual", "override needs a details line of at least 2 characters")
            return False
        was_active = self._manual is not None
        self._manual = (details, state) if enabled else None
        if was_active != enabled:
            self.log("info", "manual", "override enabled" if enabled else "override released")
            self.manual_changed.emit(enabled)
        self._reevaluate()
        return True

    def set_paused(self, paused: bool) -> None:
        if paused == self.paused:
            return
        self.paused = paused
        self.log("info", "engine", "presence paused" if paused else "presence resumed")
        self.paused_changed.emit(paused)
        self._reevaluate()

    def reload_rules(self) -> None:
        try:
            book = RuleBook.load(self._rules_path)
        except RuleError as exc:
            self.rule_error = str(exc)
            self.log("error", "rules", f"reload failed, keeping previous rules: {exc}")
            self.rules_changed.emit(self.rulebook, self.rule_error)
            return
        self._apply_rulebook(book)
        self.log("info", "rules", f"reloaded {len(book.rules)} rules")

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> None:
        try:
            RuleBook.persist_enabled(self._rules_path, rule_id, enabled)
        except (RuleError, OSError, ValueError) as exc:
            self.log("error", "rules", f"could not save '{rule_id}': {exc}")
            self.rules_changed.emit(self.rulebook, self.rule_error)
            return
        self._apply_rulebook(self.rulebook.with_enabled(rule_id, enabled))
        self.log("info", "rules", f"rule '{rule_id}' {'enabled' if enabled else 'disabled'}")

    def set_client_id(self, client_id: str) -> bool:
        client_id = client_id.strip()
        if not client_id.isdigit():
            self.log("error", "settings", "Application ID must contain digits only")
            return False
        self.settings.discord_client_id = client_id
        self.settings.save()
        self.log("info", "settings", "Application ID saved")
        self.client_id_changed.emit(client_id)
        return True

    def _apply_rulebook(self, book: RuleBook) -> None:
        self.rulebook = book
        self.rule_error = ""
        self.engine.rulebook = book
        self.rules_changed.emit(book, "")
        self._reevaluate()

    def _reevaluate(self) -> None:
        resolution = self.engine.resolve(
            EngineInput(
                now=time.time(),
                activities=self._activities,
                media=self._media,
                manual=self._manual,
                paused=self.paused,
                idle_seconds=self._idle_seconds,
                shared_variables=self._shared,
            )
        )
        self.resolution = resolution

        signature = (
            resolution.presence,
            resolution.rule.id if resolution.rule else None,
            resolution.live_rule_ids,
            resolution.sent,
            resolution.reason,
        )
        if signature != self._last_signature:
            self._last_signature = signature
            self.presence_changed.emit(resolution)

        trigger = resolution.trigger
        if trigger != self._last_trigger:
            self._last_trigger = trigger
            if resolution.rule is not None and resolution.presence is not None:
                headline = resolution.presence.details or resolution.rule.name
                self.log("trigger", f"P{int(resolution.rule.tier)}", f"{resolution.rule.name}: {headline}")
            else:
                self.log("info", "engine", "no active context")

        outgoing = resolution.presence if resolution.sent else None
        if outgoing != self._last_outgoing:
            self._last_outgoing = outgoing
            self.outgoing_changed.emit(outgoing)

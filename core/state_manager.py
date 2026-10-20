"""Central reactive state: wires workers, Privacy Shield, Mood Engine, Celestial Sync, Priority Engine, and UI."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from core.analytics_manager import ANALYTICS_MANAGER
from core.priority_engine import (
    CustomRPCConfig,
    EngineInput,
    PresenceData,
    PriorityEngine,
    Resolution,
    RuleBook,
    RuleError,
)
from core.privacy_shield import PRIVACY_SHIELD, PrivacyShield
from core.settings_manager import CLIENT_ID_ENV, DEFAULT_CLIENT_ID, SETTINGS_MANAGER, Settings
from extractors.celestial_sync import CELESTIAL_SYNC
from extractors.github_resolver import GITHUB_AVATAR_RESOLVER
from extractors.mood_engine import MOOD_ENGINE
from extractors.youtube_resolver import YOUTUBE_THUMBNAIL_RESOLVER

__all__ = ["CLIENT_ID_ENV", "DEFAULT_CLIENT_ID", "LogEntry", "Settings", "StateManager"]


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
    custom_rpc_changed = Signal(bool, object)
    client_id_changed = Signal(str)
    profile_changed = Signal(str, str)
    profile_updated = Signal(dict)
    shield_changed = Signal(bool)
    settings_updated = Signal(object)
    analytics_changed = Signal(object)

    def __init__(
        self,
        settings: Settings,
        rules_path: Path,
        rulebook: RuleBook,
        rule_error: str = "",
        privacy_shield: PrivacyShield | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        SETTINGS_MANAGER.bind(settings)
        self._rules_path = rules_path
        self.rulebook = rulebook
        self.rule_error = rule_error

        self.privacy_shield = privacy_shield or PRIVACY_SHIELD
        self._shield_active = False

        self.engine = PriorityEngine(
            rulebook,
            settings.idle_threshold_seconds,
            settings.show_idle_presence,
            settings.show_buttons,
            mood_engine_enabled=settings.mood_engine_enabled,
            mood_apply_to_small_badge=settings.mood_apply_to_small_badge,
            celestial_sync_enabled=settings.celestial_sync_enabled,
            celestial_append_to_state=settings.celestial_append_to_state,
            privacy_show_city=settings.privacy_show_city,
            idle_custom_image=settings.idle_custom_image,
        )
        self.log_history: deque[LogEntry] = deque(maxlen=settings.log_capacity)
        self.resolution: Resolution | None = None
        self.connection: tuple[str, str] = ("disconnected", "Starting")
        self.paused = False

        self._ipc_profile: tuple[str, str] = ("", "")
        self._ipc_profile_data: dict = {}
        self._activities: tuple = ()
        self._media = None
        self._manual: CustomRPCConfig | None = None
        self._idle_seconds = 0.0
        self._shared: dict[str, str] = {}
        self._last_signature: tuple | None = None
        self._last_outgoing: Any = object()
        self._last_trigger: tuple[str, str] | None = None

        self._sync_feature_modules()

        YOUTUBE_THUMBNAIL_RESOLVER.signals.resolved.connect(self.on_youtube_thumbnail)
        GITHUB_AVATAR_RESOLVER.signals.resolved.connect(self.on_github_avatar)
        MOOD_ENGINE.signals.resolved.connect(self.on_mood_resolved)
        CELESTIAL_SYNC.signals.weather_updated.connect(self.on_celestial_updated)
        self.privacy_shield.shield_engaged.connect(self.on_shield_engaged)
        SETTINGS_MANAGER.settings_changed.connect(self._on_settings_manager_changed)

    def _sync_feature_modules(self) -> None:
        s = self.settings
        MOOD_ENGINE.set_enabled(s.mood_engine_enabled)
        active_lat = s.manual_lat if s.location_mode == "manual" and s.manual_location_name else s.celestial_latitude
        active_lon = s.manual_lon if s.location_mode == "manual" and s.manual_location_name else s.celestial_longitude
        CELESTIAL_SYNC.configure(
            enabled=s.celestial_sync_enabled,
            weather_enabled=s.celestial_weather_enabled,
            latitude=active_lat,
            longitude=active_lon,
            location_mode=s.location_mode,
            manual_location_name=s.manual_location_name,
        )
        self.privacy_shield.configure(
            enabled=s.privacy_shield_enabled,
            lock_hook=s.privacy_lock_hook,
            inactivity_minutes=s.privacy_inactivity_minutes,
        )
        self.engine.configure(
            s.idle_threshold_seconds,
            s.show_idle_presence,
            s.show_buttons,
            mood_engine_enabled=s.mood_engine_enabled,
            mood_apply_to_small_badge=s.mood_apply_to_small_badge,
            celestial_sync_enabled=s.celestial_sync_enabled,
            celestial_append_to_state=s.celestial_append_to_state,
            privacy_show_city=s.privacy_show_city,
            idle_custom_image=s.idle_custom_image,
        )

    @property
    def shield_active(self) -> bool:
        return bool(self._shield_active and self.settings.privacy_shield_enabled)

    @property
    def profile_data(self) -> dict:
        """Last full Discord profile received from IPC READY (empty until connected)."""
        return dict(self._ipc_profile_data)

    @property
    def profile(self) -> tuple[str, str]:
        """Return (display_name, handle) prioritizing settings.json overrides, then IPC READY user, then fallback."""
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
    def _on_settings_manager_changed(self, updated: Settings) -> None:
        self.settings = updated
        self._sync_feature_modules()
        if not self.settings.privacy_shield_enabled and self._shield_active:
            self._shield_active = False
            self.shield_changed.emit(False)
            self.log("info", "shield", "Privacy Shield disabled via Settings")
        self.profile_changed.emit(*self.profile)
        self.settings_updated.emit(self.settings)
        self._reevaluate()

    def update_settings(self, **changes: Any) -> Settings:
        """Update one or more settings fields, persist to disk, and immediately apply to live engines."""
        updated = SETTINGS_MANAGER.update(**changes)
        return updated

    @Slot(bool)
    def on_shield_engaged(self, engaged: bool) -> None:
        effective = bool(engaged and self.settings.privacy_shield_enabled)
        if effective == self._shield_active:
            return
        self._shield_active = effective
        if effective:
            self.log("info", "shield", "Privacy Shield engaged (Stealth Mode Active)")
        else:
            self.log("info", "shield", "Privacy Shield disengaged, restoring live presence")
        self.shield_changed.emit(effective)
        self._reevaluate()

    @Slot(str, object)
    def on_mood_resolved(self, _url: str, mood) -> None:
        if self.settings.mood_engine_enabled:
            self.log("info", "mood", f"palette classified: {mood.mood_name} ({mood.dominant_hex})")
            self._reevaluate()

    @Slot(object)
    def on_celestial_updated(self, snapshot) -> None:
        if self.settings.celestial_sync_enabled:
            self.log("info", "celestial", f"ambient sync: {snapshot.ambient_vibe}")
            self._reevaluate()

    @Slot(object)
    def on_scan(self, result) -> None:
        self._activities = result.activities
        self._idle_seconds = result.idle_seconds
        self._shared = result.telemetry.as_variables()
        self.privacy_shield.check_inactivity(self._idle_seconds)
        self.telemetry_changed.emit(result.telemetry)
        self._reevaluate()

    @Slot(object)
    def on_media(self, info) -> None:
        prev_sig = (
            (getattr(self._media, "track_hash", "") or (self._media.app_id, self._media.title, self._media.artist))
            if self._media is not None
            else None
        )
        new_sig = (
            (getattr(info, "track_hash", "") or (info.app_id, info.title, info.artist))
            if info is not None
            else None
        )
        self._media = info
        if info is None:
            self.engine.clear_media_grace()
            if prev_sig is not None:
                self.log("info", "media", "playback stopped")
        elif new_sig != prev_sig:
            self.log("info", "media", f"now playing: {info.title}" + (f" by {info.artist}" if info.artist else ""))
        self._reevaluate()

    @Slot(str, str)
    def on_watcher_message(self, level: str, text: str) -> None:
        self.log(level, "watcher", text)

    @Slot(str, str)
    def on_media_message(self, level: str, text: str) -> None:
        self.log(level, "media", text)

    @Slot(str, str, str)
    def on_youtube_thumbnail(self, _norm_key: str, video_title: str, url: str) -> None:
        self.log("info", "youtube", f"thumbnail matched: {video_title}")
        self._reevaluate()

    @Slot(str, str, str)
    def on_github_avatar(self, _norm_key: str, username: str, url: str) -> None:
        self.log("info", "github", f"avatar resolved: @{username}")
        self._reevaluate()

    @Slot(str, str)
    def on_ipc_message(self, level: str, text: str) -> None:
        self.log(level, "ipc", text)

    @Slot(str, str)
    def on_ipc_user(self, display_name: str, handle: str) -> None:
        updated = (display_name.strip(), handle.strip())
        if updated != self._ipc_profile:
            self._ipc_profile = updated
            self.profile_changed.emit(*self.profile)

    @Slot(dict)
    def on_ipc_profile(self, profile: dict) -> None:
        """Forward the full Discord profile (avatar/banner CDN URLs, accent color) to the UI."""
        if profile:
            self._ipc_profile_data = dict(profile)
            self.profile_updated.emit(self._ipc_profile_data)

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

    def set_custom_rpc(self, enabled: bool, config: CustomRPCConfig) -> bool:
        """Engage or disengage full PreMiD-style Custom RPC on P1 Manual Override."""
        if enabled and len(config.details.strip()) < 2:
            self.log("warn", "custom-rpc", "Custom RPC requires a Details line of at least 2 characters")
            return False

        custom_app_id = config.app_id.strip()
        if enabled and custom_app_id:
            if not custom_app_id.isdigit():
                self.log("error", "custom-rpc", "Custom Application ID must contain digits only")
                return False
            self.client_id_changed.emit(custom_app_id)
        elif not enabled and self._manual is not None and self._manual.app_id.strip():
            active_id = self.settings.get_active_client_id()
            if active_id:
                self.client_id_changed.emit(active_id)

        was_active = self._manual is not None
        self._manual = config if enabled else None
        if was_active != enabled:
            self.log(
                "info",
                "custom-rpc",
                f"Custom RPC engaged ({config.details})" if enabled else "Custom RPC disengaged",
            )
            self.manual_changed.emit(enabled)
        elif enabled:
            self.log("info", "custom-rpc", f"Custom RPC updated: {config.details}")
        self.custom_rpc_changed.emit(enabled, config if enabled else None)
        self._reevaluate()
        return True

    def set_manual(self, enabled: bool, details: str, state: str) -> bool:
        cfg = CustomRPCConfig(details=details.strip(), state=state.strip())
        return self.set_custom_rpc(enabled, cfg)

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

    def configure_gateway(self, use_custom: bool, custom_id: str = "") -> bool:
        """Configure Central Gateway vs Custom Application ID and reconnect IPC."""
        cleaned = custom_id.strip()
        if use_custom:
            if not cleaned.isdigit():
                self.log("error", "gateway", "Custom Application ID must contain digits only")
                return False
            self.update_settings(
                use_custom_client_id=True,
                custom_client_id=cleaned,
                client_id=cleaned,
                discord_client_id=cleaned,
            )
            active_id = self.settings.get_active_client_id()
            self.log("info", "gateway", f"Switched to Custom Application ID ({active_id})")
        else:
            self.update_settings(
                use_custom_client_id=False,
                custom_client_id=cleaned or self.settings.custom_client_id,
                client_id=DEFAULT_CLIENT_ID,
                discord_client_id=DEFAULT_CLIENT_ID,
            )
            active_id = self.settings.get_active_client_id()
            self.log("info", "gateway", f"Connected via Official OmniPresence Gateway ({active_id})")
        self.client_id_changed.emit(active_id)
        return True

    def set_client_id(self, client_id: str) -> bool:
        client_id = client_id.strip()
        if not client_id or client_id == DEFAULT_CLIENT_ID:
            return self.configure_gateway(False, self.settings.custom_client_id)
        return self.configure_gateway(True, client_id)

    def _apply_rulebook(self, book: RuleBook) -> None:
        self.rulebook = book
        self.rule_error = ""
        self.engine.rulebook = book
        self.rules_changed.emit(book, "")
        self._reevaluate()

    def _effective_manual(self) -> CustomRPCConfig | None:
        if self.shield_active:
            return CustomRPCConfig(
                details=self.settings.privacy_stealth_details or "Away from Desk \u2022 Stealth Mode Active",
                state=self.settings.privacy_stealth_state or "Session Locked \u2022 Privacy Shield Engaged",
                large_image="shield",
                large_text="Privacy Shield Engaged",
                small_image="lock",
                small_text="Biometric / Session Lock",
                timestamp_mode="elapsed",
            )
        return self._manual

    def _reevaluate(self) -> None:
        shared = dict(self._shared)
        if self.settings.celestial_sync_enabled:
            snap = CELESTIAL_SYNC.snapshot()
            shared.update(snap.as_variables())

        resolution = self.engine.resolve(
            EngineInput(
                now=time.time(),
                activities=self._activities,
                media=self._media,
                manual=self._effective_manual(),
                paused=self.paused,
                idle_seconds=self._idle_seconds,
                shared_variables=shared,
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
        trigger_changed = trigger != self._last_trigger
        if trigger_changed:
            self._last_trigger = trigger
            if resolution.rule is not None and resolution.presence is not None:
                headline = resolution.presence.details or resolution.rule.name
                self.log("trigger", f"P{int(resolution.rule.tier)}", f"{resolution.rule.name}: {headline}")
            else:
                self.log("info", "engine", "no active context")

        analytics_snap = ANALYTICS_MANAGER.record_tick(resolution, force_save=trigger_changed)
        self.analytics_changed.emit(analytics_snap)

        outgoing = resolution.presence if resolution.sent else None
        if outgoing != self._last_outgoing:
            self._last_outgoing = outgoing
            self.outgoing_changed.emit(outgoing)

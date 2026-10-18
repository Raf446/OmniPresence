"""Singleton Settings Manager for OmniPresence v2.3.0: persists and synchronizes config/settings.json."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

CLIENT_ID_ENV = "OMNIPRESENCE_CLIENT_ID"
DEFAULT_CLIENT_ID: str = "1560465175188996176"


def _coerce(value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
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
    client_id: str = DEFAULT_CLIENT_ID
    discord_client_id: str = DEFAULT_CLIENT_ID
    use_custom_client_id: bool = False
    custom_client_id: str = ""
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

    # Feature 1: Anti-AFK Privacy Shield & Biometric Lock
    privacy_shield_enabled: bool = True
    privacy_lock_hook: bool = True
    privacy_inactivity_minutes: int = 10
    privacy_stealth_details: str = "Away from Desk \u2022 Stealth Mode Active"
    privacy_stealth_state: str = "Session Locked \u2022 Privacy Shield Engaged"

    # Feature 2: Adaptive Mood Engine (HSL Cover Quantization)
    mood_engine_enabled: bool = True
    mood_apply_to_small_badge: bool = True

    # Feature 3: Ambient World & Celestial Sync
    celestial_sync_enabled: bool = True
    celestial_weather_enabled: bool = True
    location_mode: str = "gps"  # "gps" (Windows Location Sensor / IP fallback) or "manual"
    manual_location_name: str = ""
    manual_lat: float = -6.2088
    manual_lon: float = 106.8456
    celestial_latitude: float = -6.2088
    celestial_longitude: float = 106.8456
    celestial_append_to_state: bool = True
    privacy_show_city: bool = False
    idle_custom_image: str = ""

    path: Path | None = field(default=None, repr=False, compare=False)

    def get_active_client_id(self) -> str:
        if self.use_custom_client_id and self.custom_client_id.strip():
            return self.custom_client_id.strip()
        return DEFAULT_CLIENT_ID

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
        if not settings.client_id.strip():
            settings.client_id = DEFAULT_CLIENT_ID
        if not settings.discord_client_id.strip():
            settings.discord_client_id = DEFAULT_CLIENT_ID
        settings.polling_interval = min(max(settings.polling_interval, 0.25), 30.0)
        settings.idle_threshold_seconds = min(max(settings.idle_threshold_seconds, 30), 86400)
        settings.privacy_inactivity_minutes = min(max(settings.privacy_inactivity_minutes, 0), 720)
        settings.log_capacity = min(max(settings.log_capacity, 50), 5000)
        env_id = os.environ.get(CLIENT_ID_ENV, "").strip()
        if env_id and not settings.custom_client_id:
            settings.custom_client_id = env_id
        active_id = settings.get_active_client_id()
        settings.client_id = active_id
        settings.discord_client_id = active_id
        return settings

    def to_dict(self) -> dict[str, Any]:
        active_id = self.get_active_client_id()
        self.client_id = active_id
        self.discord_client_id = active_id
        return {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "path"
        }

    def save(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data.update(self.to_dict())
        payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(payload, encoding="utf-8")

        # Keep dist/config/settings.json synchronized when running in project workspace
        try:
            root = self.path.resolve().parent.parent
            dist_cfg = root / "dist" / "config" / "settings.json"
            if dist_cfg.parent.exists() and dist_cfg.resolve() != self.path.resolve():
                dist_cfg.write_text(payload, encoding="utf-8")
        except OSError:
            pass


class SettingsManager(QObject):
    """Thread-safe singleton managing OmniPresence v2.3.0 settings and broadcasting live updates."""

    settings_changed = Signal(object)

    _instance: "SettingsManager | None" = None
    _lock = threading.Lock()

    def __init__(self, path: Path | None = None, parent=None) -> None:
        super().__init__(parent)
        default_path = path or (Path(__file__).resolve().parent.parent / "config" / "settings.json")
        self._settings = Settings.load(default_path)

    @classmethod
    def instance(cls, path: Path | None = None) -> "SettingsManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(path=path)
            elif path is not None and cls._instance.settings.path != path:
                cls._instance.reload(path)
            return cls._instance

    @property
    def settings(self) -> Settings:
        return self._settings

    def reload(self, path: Path | None = None) -> Settings:
        target = path or self._settings.path or (Path(__file__).resolve().parent.parent / "config" / "settings.json")
        self._settings = Settings.load(target)
        self.settings_changed.emit(self._settings)
        return self._settings

    def bind(self, settings: Settings) -> None:
        self._settings = settings
        self.settings_changed.emit(self._settings)

    def update(self, **changes: Any) -> Settings:
        changed = False
        for key, value in changes.items():
            if hasattr(self._settings, key) and key != "path":
                if getattr(self._settings, key) != value:
                    setattr(self._settings, key, value)
                    changed = True
        self._settings.save()
        if changed:
            self.settings_changed.emit(self._settings)
        return self._settings

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self._settings, key, default)


SETTINGS_MANAGER = SettingsManager.instance()

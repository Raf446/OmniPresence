"""Linux backend: MPRIS2 (org.mpris.MediaPlayer2.*) session polling over the D-Bus session bus.

`dbus` (dbus-python) is imported lazily inside `_get_bus()` so this module can be imported, and unit
tested with a mocked bus, on any platform.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from extractors.media.base import BaseMediaExtractor, RawTrack
from extractors.media.links import spotify_track_url_from_id

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"


def _first_text(value: Any) -> str:
    """MPRIS artists arrive as a string array; titles as plain strings."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            text = str(item).strip()
            if text:
                return text
        return ""
    return str(value).strip()


class LinuxMprisExtractor(BaseMediaExtractor):
    PLATFORM = "Linux MPRIS2"

    def __init__(
        self,
        preferred_apps: list[str] | None = None,
        art_resolver=None,
        parent=None,
        bus_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(preferred_apps, art_resolver, parent)
        self._bus_factory = bus_factory
        self._bus: Any = None

    @classmethod
    def is_available(cls) -> bool:
        try:
            import dbus  # noqa: F401
        except Exception:
            return False
        return True

    def _get_bus(self) -> Any:
        if self._bus is None:
            if self._bus_factory is not None:
                self._bus = self._bus_factory()
            else:
                import dbus

                self._bus = dbus.SessionBus()
        return self._bus

    @staticmethod
    def _get_property(bus: Any, name: str, iface: str, prop: str) -> Any:
        proxy = bus.get_object(name, MPRIS_PATH)
        return proxy.Get(iface, prop, dbus_interface=PROPS_IFACE)

    def _list_players(self, bus: Any) -> list[str]:
        return [str(n) for n in bus.list_names() if str(n).startswith(MPRIS_PREFIX)]

    def _pick(self, playing: list[str]) -> str | None:
        for preferred in self._preferred:
            for name in playing:
                if preferred in name.lower():
                    return name
        return playing[0] if playing else None

    def _read_session(self) -> RawTrack | None:
        try:
            bus = self._get_bus()
            playing: list[str] = []
            for name in self._list_players(bus):
                try:
                    status = str(self._get_property(bus, name, PLAYER_IFACE, "PlaybackStatus"))
                except Exception:
                    continue
                if status == "Playing":
                    playing.append(name)
            chosen = self._pick(playing)
            if chosen is None:
                return None
            metadata = self._get_property(bus, chosen, PLAYER_IFACE, "Metadata")
            try:
                position_us = float(self._get_property(bus, chosen, PLAYER_IFACE, "Position"))
            except Exception:
                position_us = 0.0
        except Exception:
            self._bus = None  # force reconnect on the next tick
            raise
        return self.parse_metadata(chosen, metadata, position_us)

    @staticmethod
    def parse_metadata(player_name: str, metadata: Any, position_us: float = 0.0) -> RawTrack | None:
        title = _first_text(metadata.get("xesam:title"))
        if not title:
            return None
        artist = _first_text(metadata.get("xesam:artist")) or _first_text(metadata.get("xesam:albumArtist"))
        album = _first_text(metadata.get("xesam:album"))

        art_url = _first_text(metadata.get("mpris:artUrl"))
        if not art_url.lower().startswith(("http://", "https://")):
            art_url = ""  # file:// covers cannot be shown on Discord; the web resolver takes over

        track_url = spotify_track_url_from_id(_first_text(metadata.get("xesam:url"))) or spotify_track_url_from_id(
            _first_text(metadata.get("mpris:trackid"))
        )

        duration, start_at, end_at = 0.0, None, None
        try:
            span = float(metadata.get("mpris:length", 0)) / 1_000_000.0
        except (TypeError, ValueError):
            span = 0.0
        if span > 1:
            now = time.time()
            position = min(max(position_us / 1_000_000.0, 0.0), span)
            duration, start_at, end_at = span, now - position, now - position + span

        app_id = player_name[len(MPRIS_PREFIX):] if player_name.startswith(MPRIS_PREFIX) else player_name
        app_id = app_id.split(".instance", 1)[0]
        return RawTrack(
            app_id=app_id,
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            start_at=start_at,
            end_at=end_at,
            art_url=art_url,
            track_url=track_url,
        )

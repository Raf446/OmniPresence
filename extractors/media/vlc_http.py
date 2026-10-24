"""VLC detection through VLC's built-in HTTP web interface (`/requests/status.json`), as in `vlc-rpc`.

Enable it in VLC: Tools > Preferences > Show All > Interface > Main interfaces > tick "Web", then
Main interfaces > Lua > Lua HTTP > Password. Put the same password in `settings.json` (`vlc_http_password`).
VLC uses HTTP Basic Auth with an empty username.

The client never raises: a closed VLC, a disabled web interface, a timeout, a wrong password or garbage
JSON all simply yield `None`, so the presence falls back to the next priority.
"""
from __future__ import annotations

import base64
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from extractors.media.base import RawTrack
from extractors.media.common import (
    AUDIO_EXTENSIONS,
    MEDIA_KIND_AUDIO,
    MEDIA_KIND_VIDEO,
    STATUS_PAUSED,
    STATUS_PLAYING,
    VIDEO_EXTENSIONS,
    classify_media_kind,
    sanitize_media_title,
    split_media_extension,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_PASSWORD = "omnipresence"
POLL_INTERVAL_MS = 250
REQUEST_TIMEOUT_SECONDS = 1.0
STATUS_PATH = "/requests/status.json"


@dataclass(frozen=True)
class VlcHttpConfig:
    enabled: bool = True
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    password: str = DEFAULT_PASSWORD

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{STATUS_PATH}"


def config_from_settings() -> VlcHttpConfig:
    """Read the live settings so changes in settings.json apply without a restart."""
    try:
        from core.settings_manager import SETTINGS_MANAGER

        s = SETTINGS_MANAGER.settings
        return VlcHttpConfig(
            enabled=bool(getattr(s, "vlc_http_enabled", True)),
            host=str(getattr(s, "vlc_http_host", DEFAULT_HOST) or DEFAULT_HOST).strip(),
            port=int(getattr(s, "vlc_http_port", DEFAULT_PORT)),
            password=str(getattr(s, "vlc_http_password", DEFAULT_PASSWORD)),
        )
    except Exception:
        return VlcHttpConfig()


def _meta_text(meta: dict, *keys: str) -> str:
    for key in keys:
        value = meta.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _to_seconds(value: Any) -> float:
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _has_video_stream(category: dict) -> bool:
    for key, value in category.items():
        if key != "meta" and isinstance(value, dict) and str(value.get("Type", "")).lower() == "video":
            return True
    return False


class VlcHttpClient:
    """Polls VLC's status.json and converts it to a `RawTrack` (or None)."""

    def __init__(
        self,
        config_provider: Callable[[], VlcHttpConfig] | None = None,
        opener: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config_provider = config_provider or config_from_settings
        # An explicit empty ProxyHandler: localhost traffic must never go through a system proxy.
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._clock = clock
        self.reachable = False  # True when VLC's web interface answered the last request
        self.last_error = ""  # "", "unreachable", "timeout", "auth", "invalid"
        self._notice: str | None = None

    def pop_notice(self) -> str | None:
        """One-shot human readable warning (e.g. wrong password); None when there is nothing new."""
        notice, self._notice = self._notice, None
        return notice

    def _set_error(self, kind: str) -> None:
        self.reachable = kind == "invalid"  # garbage JSON still means something answered
        if kind == "auth" and self.last_error != "auth":
            self._notice = "VLC web interface rejected the password: check vlc_http_password in settings.json"
        self.last_error = kind

    def _request_json(self, config: VlcHttpConfig) -> dict | None:
        token = base64.b64encode(f":{config.password}".encode("utf-8")).decode("ascii")
        request = urllib.request.Request(
            config.url,
            headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            self._set_error("auth" if exc.code in (401, 403) else "invalid")
            return None
        except (socket.timeout, TimeoutError):
            self._set_error("timeout")
            return None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            self._set_error("timeout" if isinstance(reason, (socket.timeout, TimeoutError)) else "unreachable")
            return None
        except (ConnectionError, OSError, ValueError):
            self._set_error("unreachable")  # includes ConnectionRefusedError / reset / bad URL
            return None
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            self._set_error("invalid")
            return None
        if not isinstance(data, dict):
            self._set_error("invalid")
            return None
        self.reachable = True
        self.last_error = ""
        return data

    def fetch(self) -> RawTrack | None:
        config = self._config_provider()
        if not config.enabled:
            self.reachable = False
            return None
        try:
            data = self._request_json(config)
            return None if data is None else self.parse_status(data, self._clock())
        except Exception:  # defensive: a malformed payload must never take the media poller down
            self.last_error = "invalid"
            return None

    @staticmethod
    def parse_status(data: dict, now: float | None = None) -> RawTrack | None:
        state = str(data.get("state", "")).lower()
        if state == "playing":
            status = STATUS_PLAYING
        elif state == "paused":
            status = STATUS_PAUSED
        else:
            return None  # stopped (or unknown): release the presence

        now = time.time() if now is None else now
        information = data.get("information")
        category = information.get("category") if isinstance(information, dict) else None
        category = category if isinstance(category, dict) else {}
        meta = category.get("meta")
        meta = meta if isinstance(meta, dict) else {}

        filename = _meta_text(meta, "filename")
        title = _meta_text(meta, "title", "filename")
        if not title:
            return None
        artist = _meta_text(meta, "artist", "album_artist")
        album = _meta_text(meta, "album")

        ext = split_media_extension(filename)[1] or split_media_extension(title)[1]
        if ext in AUDIO_EXTENSIONS:
            kind = MEDIA_KIND_AUDIO
        elif ext in VIDEO_EXTENSIONS:
            kind = MEDIA_KIND_VIDEO
        elif _has_video_stream(category):
            kind = MEDIA_KIND_VIDEO
        else:
            kind = classify_media_kind(title, "", artist, album)

        if kind == MEDIA_KIND_VIDEO or split_media_extension(title)[1]:
            title = sanitize_media_title(title)
        if kind == MEDIA_KIND_VIDEO:
            artist = ""  # tag artists on films are noise; Discord shows the title only

        position = _to_seconds(data.get("time"))
        length = _to_seconds(data.get("length"))
        duration, start_at, end_at = 0.0, None, None
        if length > 1:
            position = min(position, length)
            duration, start_at, end_at = length, now - position, now - position + length
        else:
            start_at = now - position  # live stream / unknown length: running time only

        if status != STATUS_PLAYING:
            start_at = end_at = None  # paused: no progress bar, no ticking clock

        return RawTrack(
            app_id="vlc",
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            start_at=start_at,
            end_at=end_at,
            status=status,
            kind=kind,
        )

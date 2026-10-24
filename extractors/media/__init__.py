"""Media detection package with a Platform Abstraction Layer.

Only the backend for the running OS is imported (lazily), so `dbus` is never touched on Windows and
`winsdk` is never touched on Linux.
"""
from __future__ import annotations

import sys
from typing import Any

from extractors.media.base import POLL_INTERVAL_MS, STOP_EMPTY_TICKS_THRESHOLD, BaseMediaExtractor, RawTrack
from extractors.media.common import ALBUM_ART_RESOLVER, AlbumArtResolver, MediaInfo, make_track_hash
from extractors.media.links import build_genius_url, build_media_buttons, build_spotify_url
from extractors.media.vlc_http import VlcHttpClient, VlcHttpConfig, config_from_settings

__all__ = [
    "ALBUM_ART_RESOLVER",
    "AlbumArtResolver",
    "BaseMediaExtractor",
    "MediaInfo",
    "MediaScrobbler",
    "POLL_INTERVAL_MS",
    "RawTrack",
    "STOP_EMPTY_TICKS_THRESHOLD",
    "VlcHttpClient",
    "VlcHttpConfig",
    "build_genius_url",
    "build_media_buttons",
    "build_spotify_url",
    "config_from_settings",
    "create_media_extractor",
    "get_extractor_class",
    "make_track_hash",
]


def get_extractor_class(platform: str | None = None) -> type[BaseMediaExtractor]:
    """Return the media extractor class for `platform` (defaults to sys.platform)."""
    plat = platform or sys.platform
    if plat.startswith("win"):
        from extractors.media.windows_smtc import WindowsSmtcExtractor

        return WindowsSmtcExtractor
    if plat.startswith("linux"):
        from extractors.media.linux_mpris import LinuxMprisExtractor

        return LinuxMprisExtractor
    raise NotImplementedError(f"media detection is not supported on platform '{plat}'")


def create_media_extractor(
    preferred_apps: list[str] | None = None,
    art_resolver: AlbumArtResolver | None = None,
    parent=None,
) -> BaseMediaExtractor:
    return get_extractor_class()(preferred_apps=preferred_apps, art_resolver=art_resolver, parent=parent)


def __getattr__(name: str) -> Any:
    # `MediaScrobbler` stays a stable public alias that resolves to the active platform backend.
    if name == "MediaScrobbler":
        return get_extractor_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

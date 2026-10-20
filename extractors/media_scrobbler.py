"""Backward-compatible shim: media logic now lives in the `extractors.media` package (v2.4)."""
from __future__ import annotations

from typing import Any

from extractors.media import get_extractor_class
from extractors.media.base import POLL_INTERVAL_MS, STOP_EMPTY_TICKS_THRESHOLD, BaseMediaExtractor, RawTrack
from extractors.media.common import *  # noqa: F401,F403
from extractors.media.common import ALBUM_ART_RESOLVER, AlbumArtResolver, MediaInfo, make_track_hash  # noqa: F401

POLL_FALLBACK_SECONDS = POLL_INTERVAL_MS / 1000.0


def __getattr__(name: str) -> Any:
    if name == "MediaScrobbler":
        return get_extractor_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

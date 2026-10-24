"""Platform Abstraction Layer for media detection: shared 5-second polling, smart hash delta and artwork resolution."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, QTimer, Signal

from extractors.media.common import (
    ALBUM_ART_RESOLVER,
    TARGET_ARTWORK_SIZE,
    AlbumArtResolver,
    MediaInfo,
    MEDIA_KIND_VIDEO,
    SEEK_TOLERANCE_SECONDS,
    STATUS_PLAYING,
    fallback_artwork_for_app,
    make_track_hash,
    sanitize_artist,
    sanitize_artwork_url,
    sanitize_title,
)

POLL_INTERVAL_MS = 5000
STOP_EMPTY_TICKS_THRESHOLD = 2


@dataclass(frozen=True)
class RawTrack:
    """Platform-neutral metadata snapshot returned by a backend's `_read_session()`."""

    app_id: str
    title: str
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    start_at: float | None = None
    end_at: float | None = None
    art_url: str = ""
    track_url: str = ""
    status: str = STATUS_PLAYING
    kind: str = ""

    @property
    def state_hash(self) -> str:
        """Track identity that flips on track/title/artist/artwork change or Playing <-> Paused transition,
        while ignoring linear playback time progression (time += 1).
        """
        suffix = "" if self.status == STATUS_PLAYING else f"\x00{self.status.lower()}"
        art = f"\x00{self.art_url}" if self.art_url else ""
        return make_track_hash(self.artist, f"{self.title}{suffix}{art}")


class BaseMediaExtractor(QObject):
    """Polls the OS media session every 5 seconds and emits `media_changed` only on real changes.

    Backends implement `_read_session()` (blocking, called on a worker thread). Everything else, the
    artist+title hash comparison, the 2-tick (10s) stop confirmation, artwork lookup, is shared so
    Windows and Linux behave identically and never flap between P5 and P6.
    """

    POLL_INTERVAL_MS = POLL_INTERVAL_MS
    STOP_EMPTY_TICKS_THRESHOLD = STOP_EMPTY_TICKS_THRESHOLD
    PLATFORM = "generic"

    media_changed = Signal(object)
    message = Signal(str, str)

    def __init__(
        self,
        preferred_apps: list[str] | None = None,
        art_resolver: AlbumArtResolver | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._preferred = [app.lower() for app in (preferred_apps or [])]
        self._art = art_resolver or ALBUM_ART_RESOLVER
        self._halt = threading.Event()
        self._state_lock = threading.Lock()
        self._polling = False
        self._executor: ThreadPoolExecutor | None = None
        self._last_info: MediaInfo | None = None
        self._last_sent_hash = ""
        self._empty_ticks = 0
        self._request_seq = 0

        self._poll_timer = QTimer(self)
        self._poll_timer.setSingleShot(False)
        self._poll_timer.setInterval(self.POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._on_tick)

    # ----- backend contract -------------------------------------------------------------------
    @classmethod
    def is_available(cls) -> bool:
        return True

    def _read_session(self) -> RawTrack | None:
        """Return the actively playing track or None. Blocking; runs on the polling worker thread."""
        raise NotImplementedError

    # ----- public interface -------------------------------------------------------------------
    def start(self) -> None:
        if self._poll_timer.isActive():
            return
        if not self.is_available():
            self.message.emit("warn", f"{self.PLATFORM} media backend is unavailable: media detection is disabled")
            return
        self._halt.clear()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-poll")
        self.message.emit(
            "info",
            f"polling {self.PLATFORM} media sessions every {self.POLL_INTERVAL_MS / 1000:.1f}s (periodic anti-flap mode)",
        )
        self._poll_timer.start()
        self._on_tick()

    def stop(self) -> None:
        self._halt.set()
        self._poll_timer.stop()
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def get_current_track(self) -> MediaInfo | None:
        with self._state_lock:
            return self._last_info

    # ----- polling ----------------------------------------------------------------------------
    def _on_tick(self) -> None:
        executor = self._executor
        if self._halt.is_set() or executor is None:
            return
        with self._state_lock:
            if self._polling:
                return
            self._polling = True
        try:
            executor.submit(self._poll_once)
        except RuntimeError:
            with self._state_lock:
                self._polling = False

    def _poll_once(self) -> None:
        try:
            try:
                raw = self._read_session()
            except Exception as exc:
                self.message.emit("warn", f"media poll failed: {exc!r}")
                return
            if self._halt.is_set():
                return
            if raw is None:
                self._publish(None)
                return
            track_hash = raw.state_hash
            with self._state_lock:
                unchanged = track_hash == self._last_sent_hash and self._last_info is not None
                if unchanged and not self._timeline_shifted(self._last_info, raw):
                    self._empty_ticks = 0
                    return
            # New track, Playing <-> Paused transition, or a seek/stale timeline: always push a dirty update.
            self._publish(self._build_info(raw, track_hash), force=True)
        finally:
            with self._state_lock:
                self._polling = False

    def _build_info(self, raw: RawTrack, track_hash: str) -> MediaInfo:
        with self._state_lock:
            self._request_seq += 1
            request_id = self._request_seq

        artwork = sanitize_artwork_url(raw.art_url)
        if not artwork and raw.kind == MEDIA_KIND_VIDEO:
            try:
                from extractors.media.poster_resolver import POSTER_RESOLVER

                resolved = POSTER_RESOLVER.resolve_for_engine(raw.title)
                artwork = sanitize_artwork_url(resolved)
            except Exception:
                artwork = ""
        elif not artwork:
            cached, cached_url = self._art.get_cached(raw.artist, raw.title, raw.album)
            if cached:
                artwork = sanitize_artwork_url(cached_url)
            else:
                try:
                    artwork = sanitize_artwork_url(
                        self._art.fetch_sync(raw.artist, raw.title, raw.album, is_stale=self._halt.is_set)
                    )
                except Exception:
                    artwork = ""
                if artwork:
                    label = f"{sanitize_artist(raw.artist)} - {sanitize_title(raw.title)}".strip(" -")
                    self.message.emit("info", f"album cover matched ({TARGET_ARTWORK_SIZE}): {label}")
                else:
                    self.message.emit(
                        "info",
                        f"cover score below threshold or unavailable for '{raw.title}', "
                        f"using fallback icon ({fallback_artwork_for_app(raw.app_id)})",
                    )
        if artwork:
            try:
                from extractors.mood_engine import MOOD_ENGINE

                if MOOD_ENGINE.enabled:
                    MOOD_ENGINE.resolve_for_url(artwork, background=False)
            except Exception:
                pass

        return MediaInfo(
            app_id=raw.app_id,
            title=raw.title,
            artist=raw.artist,
            album=raw.album,
            duration=raw.duration,
            start_at=raw.start_at,
            end_at=raw.end_at,
            artwork_url=artwork,
            request_id=request_id,
            track_hash=track_hash,
            track_url=raw.track_url,
            status=raw.status,
            kind=raw.kind,
        )

    @staticmethod
    def is_manual_seek(
        current_position: float,
        expected_position: float,
        tolerance: float = SEEK_TOLERANCE_SECONDS,
    ) -> bool:
        """Return True when playback position jumped by more than `tolerance` seconds (default 2.0s)."""
        return abs(float(current_position) - float(expected_position)) > tolerance

    @staticmethod
    def _timeline_shifted(last: MediaInfo, raw: RawTrack) -> bool:
        """True when a still-playing track moved on the timeline by > 2.0s (manual seek, not linear time += 1)."""
        if raw.status != STATUS_PLAYING or raw.start_at is None or last.start_at is None:
            return False
        return abs(raw.start_at - last.start_at) > SEEK_TOLERANCE_SECONDS

    def _publish(self, info: MediaInfo | None, force: bool = False) -> None:
        """Emit only on a new track, or on stop after STOP_EMPTY_TICKS_THRESHOLD consecutive empty ticks."""
        if info is None:
            with self._state_lock:
                if self._last_info is None:
                    self._empty_ticks = 0
                    return
                self._empty_ticks += 1
                if self._empty_ticks < self.STOP_EMPTY_TICKS_THRESHOLD:
                    return
                self._empty_ticks = 0
                self._last_sent_hash = ""
                self._last_info = None
            self.media_changed.emit(None)
            return

        track_hash = info.track_hash or make_track_hash(info.artist, info.title)
        with self._state_lock:
            self._empty_ticks = 0
            if not force and track_hash == self._last_sent_hash and self._last_info is not None:
                return
            info = replace(info, track_hash=track_hash)
            self._last_sent_hash = track_hash
            self._last_info = info
        self.media_changed.emit(info)

    @staticmethod
    def now() -> float:
        return time.time()

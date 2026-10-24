"""Asynchronous, non-blocking YouTube video thumbnail resolver (no API key required)."""
from __future__ import annotations

import gzip
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

YOUTUBE_SEARCH_URL = "https://www.youtube.com/results?search_query={query}"
YOUTUBE_THUMB_TEMPLATE = "https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
DEFAULT_FALLBACK_ASSET = "youtube"
HTTP_TIMEOUT_SECONDS = 1.8
NEGATIVE_CACHE_TTL_SECONDS = 45.0

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_VIDEO_ID_RE = re.compile(r'"videoId":"([a-zA-Z0-9_-]{11})"')
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_LEADING_NOISE_RE = re.compile(
    r"^(?:(?:\(\d+\)|\[\d+\])\s*|(?:[▶►▸▹⏵⏸⏹⏺🔊🔉🔈🔇🔴🟢🎵🎶🎧📺]\ufe0f?|\u25b6\ufe0f?|\u23f8\ufe0f?)\s*)+"
)
_EMOJI_NOISE_RE = re.compile(
    r"[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u25ff\ufe0f]+"
)
_MULTI_SPACE_RE = re.compile(r"\s+")


def clean_video_query(video_title: str) -> str:
    """Remove zero-width chars, leading play/notification glyphs, emojis, and duplicate spaces."""
    if not video_title:
        return ""
    cleaned = _ZERO_WIDTH_RE.sub("", video_title)
    cleaned = _LEADING_NOISE_RE.sub("", cleaned)
    cleaned = _EMOJI_NOISE_RE.sub(" ", cleaned)
    return _MULTI_SPACE_RE.sub(" ", cleaned).strip(" -–—|•\t")


def normalize_video_key(video_title: str) -> str:
    """Normalized cache key for in-memory thumbnail dictionary."""
    return clean_video_query(video_title).lower()


class _ResolverSignals(QObject):
    """Qt signals emitted when a background QThreadPool thumbnail lookup finishes."""

    resolved = Signal(str, str, str)  # (normalized_key, video_title, thumbnail_url)


class _ThumbnailRunnable(QRunnable):
    """QThreadPool worker task that fetches a YouTube thumbnail URL with a strict 1.8s timeout."""

    def __init__(
        self,
        resolver: "YouTubeThumbnailResolver",
        norm_key: str,
        video_title: str,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._resolver = resolver
        self._norm_key = norm_key
        self._video_title = video_title

    def run(self) -> None:
        url = DEFAULT_FALLBACK_ASSET
        try:
            url = self._resolver.fetch_sync(self._video_title)
        except Exception:
            url = DEFAULT_FALLBACK_ASSET
        finally:
            self._resolver._on_task_finished(self._norm_key, self._video_title, url, self)


class YouTubeThumbnailResolver:
    """Resolves YouTube video titles to high-res thumbnail URLs via QThreadPool with in-memory caching."""

    def __init__(self, timeout: float = HTTP_TIMEOUT_SECONDS) -> None:
        self._timeout = min(max(timeout, 0.5), HTTP_TIMEOUT_SECONDS)
        self._lock = threading.Lock()
        self.cache: dict[str, str] = {}
        self._failed_at: dict[str, float] = {}
        self._pending: set[str] = set()
        self._active_runnables: set[_ThumbnailRunnable] = set()
        self._listeners: list[Callable[[str, str], None]] = []
        self.signals = _ResolverSignals()
        self._pool = QThreadPool.globalInstance()

    def add_listener(self, callback: Callable[[str, str], None]) -> None:
        """Register a callback(video_title, thumbnail_url) invoked when an async lookup resolves."""
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def get_cached(self, video_title: str) -> tuple[bool, str]:
        """Return (is_cached, url_or_fallback) without performing network I/O."""
        key = normalize_video_key(video_title)
        if not key:
            return True, DEFAULT_FALLBACK_ASSET
        now = time.monotonic()
        with self._lock:
            if key in self.cache:
                return True, self.cache[key]
            failed_ts = self._failed_at.get(key)
            if failed_ts is not None:
                if (now - failed_ts) < NEGATIVE_CACHE_TTL_SECONDS:
                    return True, DEFAULT_FALLBACK_ASSET
                del self._failed_at[key]
        return False, DEFAULT_FALLBACK_ASSET

    def fetch_sync(self, video_title: str) -> str:
        """Synchronously resolve a video title to https://i.ytimg.com/vi/{id}/hqdefault.jpg or 'youtube'."""
        cleaned = clean_video_query(video_title)
        key = cleaned.lower()
        if not cleaned:
            return DEFAULT_FALLBACK_ASSET

        with self._lock:
            if key in self.cache:
                return self.cache[key]

        encoded = urllib.parse.quote_plus(cleaned)
        url = YOUTUBE_SEARCH_URL.format(query=encoded)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": BROWSER_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
                "Accept-Encoding": "gzip",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                if resp.status != 200:
                    self._record_failure(key)
                    return DEFAULT_FALLBACK_ASSET
                raw_bytes = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw_bytes = gzip.decompress(raw_bytes)
                raw_html = raw_bytes.decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            self._record_failure(key)
            return DEFAULT_FALLBACK_ASSET

        match = _VIDEO_ID_RE.search(raw_html)
        if not match:
            self._record_failure(key)
            return DEFAULT_FALLBACK_ASSET

        video_id = match.group(1)
        thumb_url = YOUTUBE_THUMB_TEMPLATE.format(video_id=video_id)
        with self._lock:
            self.cache[key] = thumb_url
            self._failed_at.pop(key, None)
        return thumb_url

    def resolve(self, video_title: str, *, background: bool = False) -> str:
        """Resolve a YouTube video title to its thumbnail URL.

        - When `background=True` (used by the live presence engine), returns immediately from
          the in-memory cache or schedules a non-blocking `QThreadPool` lookup and returns `'youtube'`.
        - When `background=False` (default for direct calls/assertions), executes the lookup
          with the strict 1.8s timeout and returns the resolved thumbnail URL or `'youtube'`.
        """
        if background:
            return self.resolve_async(video_title)
        return self.fetch_sync(video_title)

    def resolve_async(self, video_title: str) -> str:
        """Non-blocking lookup: return cached thumbnail URL immediately or dispatch a QThreadPool worker."""
        key = normalize_video_key(video_title)
        if not key:
            return DEFAULT_FALLBACK_ASSET

        cached, url = self.get_cached(video_title)
        if cached:
            return url

        with self._lock:
            if key in self._pending:
                return DEFAULT_FALLBACK_ASSET
            self._pending.add(key)
            runnable = _ThumbnailRunnable(self, key, video_title)
            self._active_runnables.add(runnable)

        self._pool.start(runnable)
        return DEFAULT_FALLBACK_ASSET

    def _record_failure(self, key: str) -> None:
        with self._lock:
            self._failed_at[key] = time.monotonic()

    def _on_task_finished(
        self,
        norm_key: str,
        video_title: str,
        url: str,
        runnable: _ThumbnailRunnable,
    ) -> None:
        with self._lock:
            self._pending.discard(norm_key)
            self._active_runnables.discard(runnable)
            listeners = list(self._listeners)
        if url and url != DEFAULT_FALLBACK_ASSET:
            try:
                self.signals.resolved.emit(norm_key, video_title, url)
            except RuntimeError:
                pass
            for callback in listeners:
                try:
                    callback(video_title, url)
                except Exception:
                    pass


YOUTUBE_THUMBNAIL_RESOLVER = YouTubeThumbnailResolver()

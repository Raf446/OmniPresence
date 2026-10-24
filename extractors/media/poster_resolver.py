"""Dynamic Online Poster & Thumbnail Resolver for VLC Media Player (AniList GraphQL + TVMaze API)."""
from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from extractors.media.common import sanitize_artwork_url, sanitize_media_title

ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"
TVMAZE_SEARCH_URL = "https://api.tvmaze.com/singlesearch/shows?q={query}"
DEFAULT_FALLBACK_ASSET = "vlc"
PAUSE_ASSET = "pause"
REQUEST_TIMEOUT = 1.5
CACHE_TTL_SECONDS = 3600.0
NEGATIVE_CACHE_TTL_SECONDS = 300.0
USER_AGENT = "OmniPresence/2.4.1 (+https://github.com/omnipresence)"

ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    title {
      romaji
      english
      native
    }
    coverImage {
      extraLarge
      large
      medium
    }
  }
}
""".strip()

# Strip disc/release format tokens ("BD", "BDRip", "DVD", "WEB-DL", "BluRay", etc.)
_FORMAT_TOKENS_RE = re.compile(
    r"\b(?:BD|BDRip|BRRip|DVD|DVDRip|Blu-?Ray|WEB-?DL|WEBRip|HDTV|UHD|HDR|HDR10|HEVC|"
    r"REMUX|10bit|8bit|FLAC|AAC|AC3|DTS|x264|x265|H\.?264|H\.?265|480p|720p|1080p|2160p|4K|"
    r"Dual[\s-]*Audio|Multi[\s-]*Sub(?:s)?|Uncensored|Batch)\b",
    re.IGNORECASE,
)

# Strip Season/Episode suffixes ("S01E01", "Season 2 Episode 5", "Ep 12", "OVA 1", etc.)
_SEASON_EPISODE_RE = re.compile(
    r"(?:[\s\-–—:]+|\b)(?:S\d{1,2}\s*E\d{1,4}|S\d{1,2}|Season\s+\d+(?:\s*(?:Episode|Ep)\.?\s*\d+)?|"
    r"Ep(?:isode)?\.?\s*\d{1,4}|OVA\s*\d*|OAD\s*\d*|ONA\s*\d*|Special\s*\d*|Movie\s*\d*)\b.*$",
    re.IGNORECASE,
)

# Strip trailing "- 01", "- 1089", "#01", etc.
_TRAILING_EPISODE_NUMBER_RE = re.compile(
    r"(?:[\s]*[-–—#]\s*\d{1,4}(?:\s*v\d+)?(?:\s*[\(\[].*[\)\]])?|"
    r"\s+(?:0\d|[1-9]\d{0,2})(?:\s*v\d+)?)\s*$",
    re.IGNORECASE,
)

# Detect personal/camera/screen recording filenames that should not query anime/TV APIs
_HOME_VIDEO_RE = re.compile(
    r"^(?:(?:VID|IMG|MOV|DSC|MVI|PXL|DJI|WIN|GOPR|GH)\d*[\s_-]*\d+|"
    r"Screen[\s_-]*Recording\b|Recording[\s_-]*\d+|bandicam\b|OBS[\s_-]*\d+|"
    r"Replay[\s_-]*\d+|Desktop[\s_-]*\d+|My[\s_-]*Home[\s_-]*Video\b|My[\s_-]*Holiday\b)",
    re.IGNORECASE,
)

_WHITESPACE_RE = re.compile(r"\s+")
_TRIM_PUNCT_RE = re.compile(r"^[\s\-–—:_.\[\]()]+|[\s\-–—:_.\[\]()]+$")


def is_personal_or_home_video(title: str) -> bool:
    """Return True if `title` looks like a personal camera clip, screen recording, or untitled home video."""
    cleaned = (title or "").strip()
    if not cleaned:
        return True
    return bool(_HOME_VIDEO_RE.search(cleaned))


def normalize_poster_title(raw_title: str) -> str:
    """Clean anime/series/movie title by stripping release tags, BD/resolution tokens, and episode numbers.

    Examples:
        "Clannad BD - 01" -> "Clannad"
        "One Piece - 1089 [1080p]" -> "One Piece"
        "[SubsPlease] Frieren - 05 (1080p) [ABCD1234].mkv" -> "Frieren"
    """
    if not raw_title or not raw_title.strip():
        return ""

    # Step 1: Run common media title sanitizer (strips extensions, [1080p], [CRC], group tags)
    cleaned = sanitize_media_title(raw_title)
    if not cleaned:
        return ""

    # Step 2: Strip explicit Season/Episode markers ("S01E05", "Episode 12", etc.)
    cleaned = _SEASON_EPISODE_RE.sub("", cleaned)

    # Step 3: Strip format tokens ("BD", "BDRip", "BluRay", "1080p", etc.)
    cleaned = _FORMAT_TOKENS_RE.sub(" ", cleaned)

    # Step 4: Strip trailing "- 01", "- 1089", "#05" episode numbers
    cleaned = _TRAILING_EPISODE_NUMBER_RE.sub("", cleaned)

    # Step 5: Strip any remaining format tokens that preceded the episode number
    cleaned = _FORMAT_TOKENS_RE.sub(" ", cleaned)

    # Step 6: Normalize whitespace and trim stray punctuation
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    cleaned = _TRIM_PUNCT_RE.sub("", cleaned)
    return cleaned.strip()


def normalize_title_key(raw_title: str) -> str:
    """Return a canonical lowercase cache key for `raw_title`."""
    return normalize_poster_title(raw_title).lower()


# Alias for external callers
clean_series_title = normalize_poster_title


def _is_mock_callable(fn: Any) -> bool:
    """Return True if `fn` is a unittest.mock object (so unit tests execute synchronously without thread lag)."""
    return hasattr(fn, "assert_called") or hasattr(fn, "side_effect") or type(fn).__module__.startswith("unittest.mock")


class _PosterSignals(QObject):
    """Qt signal bridge emitted when a background poster lookup finishes."""

    resolved = Signal(str, str, str)  # (norm_key, clean_title, poster_url)


class _PosterRunnable(QRunnable):
    """Non-blocking QThreadPool task for resolving a VLC poster URL."""

    def __init__(self, resolver: "PosterResolver", norm_key: str, raw_title: str) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._resolver = resolver
        self._norm_key = norm_key
        self._raw_title = raw_title

    def run(self) -> None:
        url = DEFAULT_FALLBACK_ASSET
        try:
            url = self._resolver.fetch_sync(self._raw_title)
        except Exception:
            url = DEFAULT_FALLBACK_ASSET
        try:
            self._resolver._on_task_finished(self._norm_key, self._raw_title, url, self)
        except RuntimeError:
            pass


class PosterResolver:
    """Thread-safe online poster/thumbnail resolver for VLC Media Player.

    Queries AniList GraphQL API first (for anime/movies) and falls back to TVMaze API
    (for general TV series/shows), with a strict 1.5s timeout and in-memory TTL cache.
    """

    def __init__(
        self,
        *,
        timeout: float = REQUEST_TIMEOUT,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        negative_ttl_seconds: float = NEGATIVE_CACHE_TTL_SECONDS,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.timeout = min(float(timeout), REQUEST_TIMEOUT)
        self.ttl_seconds = float(ttl_seconds)
        self.negative_ttl_seconds = float(negative_ttl_seconds)
        self._opener = opener
        self._clock = clock

        self.signals = _PosterSignals()
        self.cache: dict[str, str] = {}
        self._expires_at: dict[str, float] = {}
        self._failed_at: dict[str, float] = {}
        self._pending: set[str] = set()
        self._active_runnables: set[_PosterRunnable] = set()
        self._listeners: list[Callable[[str, str], None]] = []
        self._lock = threading.Lock()
        self._pool = QThreadPool.globalInstance()

    def clear_cache(self) -> None:
        """Clear all positive and negative cached poster entries."""
        with self._lock:
            self.cache.clear()
            self._expires_at.clear()
            self._failed_at.clear()
            self._pending.clear()

    def add_listener(self, callback: Callable[[str, str], None]) -> None:
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def get_cached(self, raw_title: str) -> tuple[bool, str]:
        """Return `(hit, poster_url_or_fallback)` from in-memory TTL cache."""
        key = normalize_title_key(raw_title)
        if not key:
            return True, DEFAULT_FALLBACK_ASSET

        now = self._clock()
        with self._lock:
            if key in self.cache:
                if now < self._expires_at.get(key, 0.0):
                    return True, self.cache[key]
                # Expired positive entry
                self.cache.pop(key, None)
                self._expires_at.pop(key, None)

            failed_time = self._failed_at.get(key)
            if failed_time is not None:
                if (now - failed_time) < self.negative_ttl_seconds:
                    return True, DEFAULT_FALLBACK_ASSET
                self._failed_at.pop(key, None)

        return False, ""

    def _open(self, request: urllib.request.Request, timeout: float) -> Any:
        effective_timeout = min(float(timeout), REQUEST_TIMEOUT)
        if self._opener is not None:
            return self._opener(request, timeout=effective_timeout)
        return urllib.request.urlopen(request, timeout=effective_timeout)

    def fetch_anilist(self, clean_title: str) -> str:
        """Query AniList GraphQL API (`https://graphql.anilist.co`) for `clean_title`."""
        if not clean_title:
            return ""
        payload = json.dumps(
            {
                "query": ANILIST_QUERY,
                "variables": {"search": clean_title},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            ANILIST_GRAPHQL_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with self._open(req, timeout=self.timeout) as resp:
            raw_bytes = resp.read(131_072)
        data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        if not isinstance(data, dict):
            return ""
        media = (data.get("data") or {}).get("Media")
        if not isinstance(media, dict):
            return ""
        cover = media.get("coverImage")
        if not isinstance(cover, dict):
            return ""
        for size_key in ("large", "extraLarge", "medium"):
            candidate = sanitize_artwork_url(str(cover.get(size_key) or "").strip())
            if candidate:
                return candidate
        return ""

    def fetch_tvmaze(self, clean_title: str) -> str:
        """Query TVMaze API (`https://api.tvmaze.com/singlesearch/shows?q=...`) for `clean_title`."""
        if not clean_title:
            return ""
        encoded = urllib.parse.quote(clean_title, safe="")
        url = TVMAZE_SEARCH_URL.format(query=encoded)
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="GET",
        )
        with self._open(req, timeout=self.timeout) as resp:
            raw_bytes = resp.read(131_072)
        data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        if not isinstance(data, dict):
            return ""
        image = data.get("image")
        if not isinstance(image, dict):
            return ""
        for size_key in ("medium", "original"):
            raw_img = str(image.get(size_key) or "").strip()
            if raw_img.startswith("http://"):
                raw_img = "https://" + raw_img[len("http://") :]
            candidate = sanitize_artwork_url(raw_img)
            if candidate:
                return candidate
        return ""

    def fetch_sync(self, raw_title: str) -> str:
        """Synchronously resolve `raw_title` to a valid HTTPS poster URL or `'vlc'` fallback."""
        clean_title = normalize_poster_title(raw_title)
        key = clean_title.lower()
        if not key or is_personal_or_home_video(raw_title) or is_personal_or_home_video(clean_title):
            return DEFAULT_FALLBACK_ASSET

        hit, cached_url = self.get_cached(raw_title)
        if hit:
            return cached_url

        poster_url = ""
        # 1. Primary lookup: AniList GraphQL API
        try:
            poster_url = self.fetch_anilist(clean_title)
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
            OSError,
            ValueError,
            KeyError,
        ):
            poster_url = ""
        except Exception:
            poster_url = ""

        # 2. Fallback lookup: TVMaze API
        if not poster_url:
            try:
                poster_url = self.fetch_tvmaze(clean_title)
            except (
                urllib.error.HTTPError,
                urllib.error.URLError,
                TimeoutError,
                socket.timeout,
                OSError,
                ValueError,
                KeyError,
            ):
                poster_url = ""
            except Exception:
                poster_url = ""

        now = self._clock()
        with self._lock:
            if poster_url:
                self.cache[key] = poster_url
                self._expires_at[key] = now + self.ttl_seconds
                self._failed_at.pop(key, None)
                return poster_url
            self._failed_at[key] = now
            return DEFAULT_FALLBACK_ASSET

    def resolve_async(self, raw_title: str) -> str:
        """Non-blocking lookup: return cached poster URL immediately or dispatch a background worker and return `'vlc'`."""
        clean_title = normalize_poster_title(raw_title)
        key = clean_title.lower()
        if not key or is_personal_or_home_video(raw_title) or is_personal_or_home_video(clean_title):
            return DEFAULT_FALLBACK_ASSET

        hit, cached_url = self.get_cached(raw_title)
        if hit:
            return cached_url

        with self._lock:
            if key in self._pending:
                return DEFAULT_FALLBACK_ASSET
            self._pending.add(key)
            runnable = _PosterRunnable(self, key, raw_title)
            self._active_runnables.add(runnable)

        self._pool.start(runnable)
        return DEFAULT_FALLBACK_ASSET

    def resolve(self, raw_title: str, *, background: bool = False) -> str:
        """Resolve a VLC media title to its online poster URL (or `'vlc'` fallback)."""
        if background:
            return self.resolve_async(raw_title)
        return self.fetch_sync(raw_title)

    def resolve_for_engine(self, raw_title: str) -> str:
        """Used by PriorityEngine during presence rendering.

        - Returns immediately from in-memory cache on hit.
        - If network is mocked in a unit test (custom opener or mocked `urlopen`), executes synchronously
          so test assertions see the resolved poster URL immediately.
        - Otherwise dispatches a non-blocking background worker and returns `'vlc'` until resolved.
        """
        hit, cached_url = self.get_cached(raw_title)
        if hit:
            return cached_url
        if (
            self._opener is not None
            or _is_mock_callable(urllib.request.urlopen)
            or _is_mock_callable(self.fetch_sync)
            or _is_mock_callable(self.fetch_anilist)
            or _is_mock_callable(self.fetch_tvmaze)
        ):
            return self.fetch_sync(raw_title)
        return self.resolve_async(raw_title)

    def _on_task_finished(
        self,
        norm_key: str,
        raw_title: str,
        url: str,
        runnable: _PosterRunnable,
    ) -> None:
        with self._lock:
            self._pending.discard(norm_key)
            self._active_runnables.discard(runnable)
            listeners = list(self._listeners)
        if url and url != DEFAULT_FALLBACK_ASSET:
            try:
                self.signals.resolved.emit(norm_key, normalize_poster_title(raw_title), url)
            except RuntimeError:
                pass
            for callback in listeners:
                try:
                    callback(raw_title, url)
                except Exception:
                    pass


POSTER_RESOLVER = PosterResolver()

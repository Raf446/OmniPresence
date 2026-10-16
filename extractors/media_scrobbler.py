"""Windows System Media Transport Controls listener with asynchronous dynamic Album Art fetcher (iTunes / Deezer)."""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from PySide6.QtCore import QThread, Signal

try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )

    SMTC_AVAILABLE = True
except Exception:  # winsdk missing, or not Windows
    SessionManager = None
    PlaybackStatus = None
    SMTC_AVAILABLE = False

POLL_FALLBACK_SECONDS = 1.5
SEEK_TOLERANCE_SECONDS = 3.0

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
DEEZER_SEARCH_URL = "https://api.deezer.com/search"
HTTP_TIMEOUT_SECONDS = 4.0
TARGET_ARTWORK_SIZE = "600x600"
USER_AGENT = "OmniPresence/1.0 (+https://github.com/Raf446/OmniPresence)"

_NOISE_PATTERNS = (
    re.compile(r"\s*[\(\[](?:official\s*(?:music\s*)?(?:video|audio|lyric\s*video|visualizer)|lyrics?|audio|hd|4k|remastered.*?)[\)\]]", re.I),
    re.compile(r"\s*-\s*(?:YouTube(?:\s*Music)?|Spotify|VLC media player)\s*$", re.I),
    re.compile(r"\s*(?:ft\.?|feat\.?)\s+[^-\(\[\)\]]+", re.I),
)
_ITUNES_DIM_RE = re.compile(r"/\d+x\d+(bb|sr|bf|cc)?\.(jpg|jpeg|png|webp)$", re.I)


def _clean_query_part(text: str) -> str:
    cleaned = text.strip()
    for pat in _NOISE_PATTERNS:
        cleaned = pat.sub("", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" -|•")


def upgrade_artwork_resolution(url: str, size: str = TARGET_ARTWORK_SIZE) -> str:
    """Upgrade iTunes / Deezer thumbnail URLs to high resolution (default 600x600)."""
    url = url.strip()
    if not url.lower().startswith("https://"):
        return ""
    # iTunes artwork URL pattern: .../100x100bb.jpg -> .../600x600bb.jpg
    if "mzstatic.com" in url:
        return _ITUNES_DIM_RE.sub(rf"/{size}bb.\2", url)
    # Deezer CDN URL pattern: .../250x250-000000-80-0-0.jpg -> .../600x600-000000-80-0-0.jpg
    if "dzcdn.net" in url:
        return re.sub(r"/\d+x\d+-", f"/{size}-", url)
    return url


class AlbumArtResolver:
    """Thread-safe in-memory LRU cache and rate-limited public API resolver (iTunes + Deezer fallback)."""

    def __init__(
        self,
        max_entries: int = 256,
        miss_ttl_seconds: float = 90.0,
        min_request_gap: float = 0.35,
    ) -> None:
        self._max_entries = max_entries
        self._miss_ttl = miss_ttl_seconds
        self._min_gap = min_request_gap
        self._lock = threading.Lock()
        # key -> (url, timestamp, is_hit)
        self._cache: OrderedDict[str, tuple[str, float, bool]] = OrderedDict()
        self._last_request_at = 0.0

    @staticmethod
    def make_key(artist: str, title: str) -> str:
        norm_artist = re.sub(r"\s+", " ", (artist or "").strip().lower())
        norm_title = re.sub(r"\s+", " ", (title or "").strip().lower())
        return f"{norm_artist} - {norm_title}"

    def get_cached(self, artist: str, title: str) -> tuple[bool, str]:
        """Return (found_in_cache, artwork_url). Negative entries expire after miss_ttl_seconds."""
        key = self.make_key(artist, title)
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return False, ""
            url, ts, is_hit = entry
            if not is_hit and (now - ts) >= self._miss_ttl:
                del self._cache[key]
                return False, ""
            self._cache.move_to_end(key)
            return True, url

    def _store(self, artist: str, title: str, url: str) -> None:
        key = self.make_key(artist, title)
        now = time.monotonic()
        with self._lock:
            self._cache[key] = (url, now, bool(url))
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)

    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._min_gap - (now - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def fetch_sync(self, artist: str, title: str, album: str = "") -> str:
        """Blocking lookup with LRU cache, rate-limit guard, iTunes primary, and Deezer fallback."""
        if not title.strip():
            return ""
        cached, url = self.get_cached(artist, title)
        if cached:
            return url

        clean_title = _clean_query_part(title) or title.strip()
        clean_artist = _clean_query_part(artist)

        queries: list[str] = []
        if clean_artist and clean_title:
            queries.append(f"{clean_artist} {clean_title}")
        if album.strip() and clean_title:
            queries.append(f"{clean_title} {_clean_query_part(album)}")
        if clean_title and clean_title not in queries:
            queries.append(clean_title)

        resolved_url = ""
        for query in queries:
            resolved_url = self._query_itunes(query, clean_artist, clean_title)
            if resolved_url:
                break
            resolved_url = self._query_deezer(query, clean_artist, clean_title)
            if resolved_url:
                break

        self._store(artist, title, resolved_url)
        return resolved_url

    async def fetch_async(
        self,
        artist: str,
        title: str,
        album: str = "",
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> str:
        """Non-blocking async wrapper that runs HTTP resolution in a background worker thread."""
        cached, url = self.get_cached(artist, title)
        if cached:
            return url
        active_loop = loop or asyncio.get_running_loop()
        return await active_loop.run_in_executor(None, self.fetch_sync, artist, title, album)

    def _http_json(self, url: str) -> dict | None:
        self._throttle()
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                if resp.status != 200:
                    return None
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None

    def _query_itunes(self, query: str, artist: str, title: str) -> str:
        params = urllib.parse.urlencode({"term": query, "entity": "song", "limit": 5})
        payload = self._http_json(f"{ITUNES_SEARCH_URL}?{params}")
        if not payload:
            return ""
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            return ""

        best_url = ""
        best_score = -1
        target_artist = artist.lower()
        target_title = title.lower()

        for item in results:
            if not isinstance(item, dict):
                continue
            raw_art = str(item.get("artworkUrl100") or item.get("artworkUrl60") or "").strip()
            if not raw_art:
                continue
            cand_title = str(item.get("trackName") or "").lower()
            cand_artist = str(item.get("artistName") or "").lower()
            score = 0
            if target_title and (target_title in cand_title or cand_title in target_title):
                score += 2
            if target_artist and (target_artist in cand_artist or cand_artist in target_artist):
                score += 3
            if score > best_score:
                best_score = score
                best_url = raw_art

        return upgrade_artwork_resolution(best_url, TARGET_ARTWORK_SIZE) if best_url else ""

    def _query_deezer(self, query: str, artist: str, title: str) -> str:
        params = urllib.parse.urlencode({"q": query, "limit": 5})
        payload = self._http_json(f"{DEEZER_SEARCH_URL}?{params}")
        if not payload:
            return ""
        results = payload.get("data")
        if not isinstance(results, list) or not results:
            return ""

        best_url = ""
        best_score = -1
        target_artist = artist.lower()
        target_title = title.lower()

        for item in results:
            if not isinstance(item, dict):
                continue
            album_obj = item.get("album") or {}
            artist_obj = item.get("artist") or {}
            raw_art = str(
                album_obj.get("cover_xl")
                or album_obj.get("cover_big")
                or album_obj.get("cover_medium")
                or ""
            ).strip()
            if not raw_art:
                continue
            cand_title = str(item.get("title") or "").lower()
            cand_artist = str(artist_obj.get("name") or "").lower()
            score = 0
            if target_title and (target_title in cand_title or cand_title in target_title):
                score += 2
            if target_artist and (target_artist in cand_artist or cand_artist in target_artist):
                score += 3
            if score > best_score:
                best_score = score
                best_url = raw_art

        return upgrade_artwork_resolution(best_url, TARGET_ARTWORK_SIZE) if best_url else ""


# Shared singleton resolver used by MediaScrobbler and testable from CLI
ALBUM_ART_RESOLVER = AlbumArtResolver()


@dataclass(frozen=True)
class MediaInfo:
    app_id: str
    title: str
    artist: str
    album: str
    duration: float
    start_at: float | None
    end_at: float | None
    artwork_url: str = ""

    @property
    def track_key(self) -> tuple[str, str, str, str]:
        return (self.app_id, self.title, self.artist, self.album)

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        return (self.app_id, self.title, self.artist, self.album, self.artwork_url)


class MediaScrobbler(QThread):
    """Runs an asyncio loop that watches SMTC sessions, fetches album art asynchronously, and emits the active track."""

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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._hooked: set[str] = set()
        self._handlers: list = []
        self._last_identity: tuple | None = None
        self._last_start: float | None = None
        self._pending_art_keys: set[str] = set()
        self._bg_tasks: set[asyncio.Task] = set()

    def stop(self) -> None:
        self._halt.set()
        self._poke()
        self.wait(3000)

    def _poke(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None and not loop.is_closed():
            loop.call_soon_threadsafe(wake.set)

    def run(self) -> None:
        if not SMTC_AVAILABLE:
            self.message.emit("warn", "winsdk is unavailable: media detection is disabled")
            return
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._watch())
        except Exception as exc:
            self.message.emit("error", f"media listener stopped: {exc!r}")
        finally:
            for task in list(self._bg_tasks):
                task.cancel()
            self._bg_tasks.clear()
            self._loop = None
            loop.close()

    async def _watch(self) -> None:
        self._wake = asyncio.Event()
        manager = await SessionManager.request_async()
        self._subscribe_manager(manager)
        self.message.emit("info", "listening for Windows media sessions (dynamic album art enabled)")
        while not self._halt.is_set():
            self._wake.clear()
            try:
                info = await self._snapshot(manager)
            except Exception as exc:
                self.message.emit("warn", f"media poll failed: {exc!r}")
                info = None
            self._publish(info)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=POLL_FALLBACK_SECONDS)
            except asyncio.TimeoutError:
                pass
        self.media_changed.emit(None)

    def _subscribe_manager(self, manager) -> None:
        loop, wake = self._loop, self._wake

        def on_change(*_args) -> None:
            loop.call_soon_threadsafe(wake.set)

        self._handlers.append(on_change)
        try:
            manager.add_sessions_changed(on_change)
            manager.add_current_session_changed(on_change)
        except Exception as exc:
            self.message.emit("warn", f"SMTC change events unavailable, polling only: {exc!r}")

    def _subscribe_session(self, session) -> None:
        app_id = session.source_app_user_model_id
        if app_id in self._hooked:
            return
        self._hooked.add(app_id)
        loop, wake = self._loop, self._wake

        def on_change(*_args) -> None:
            loop.call_soon_threadsafe(wake.set)

        self._handlers.append(on_change)
        try:
            session.add_playback_info_changed(on_change)
            session.add_media_properties_changed(on_change)
            session.add_timeline_properties_changed(on_change)
        except Exception:
            self._hooked.discard(app_id)

    def _pick(self, playing: list):
        for preferred in self._preferred:
            for session in playing:
                if preferred in session.source_app_user_model_id.lower():
                    return session
        return playing[0] if playing else None

    async def _snapshot(self, manager) -> MediaInfo | None:
        playing = []
        for session in manager.get_sessions():
            self._subscribe_session(session)
            playback = session.get_playback_info()
            if playback is not None and playback.playback_status == PlaybackStatus.PLAYING:
                playing.append(session)
        session = self._pick(playing)
        if session is None:
            return None

        properties = await session.try_get_media_properties_async()
        title = (properties.title or "").strip()
        if not title:
            return None
        artist = (properties.artist or properties.album_artist or "").strip()
        album = (properties.album_title or "").strip()

        duration, start_at, end_at = 0.0, None, None
        timeline = session.get_timeline_properties()
        if timeline is not None:
            span = (timeline.end_time - timeline.start_time).total_seconds()
            if span > 1:
                position = (timeline.position - timeline.start_time).total_seconds()
                drift = (datetime.now(timezone.utc) - timeline.last_updated_time).total_seconds()
                position = min(max(position + max(drift, 0.0), 0.0), span)
                now = time.time()
                duration, start_at, end_at = span, now - position, now - position + span

        # Check in-memory LRU cache first; if not cached, trigger non-blocking background fetch
        cached, artwork_url = self._art.get_cached(artist, title)
        info = MediaInfo(
            app_id=session.source_app_user_model_id,
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            start_at=start_at,
            end_at=end_at,
            artwork_url=artwork_url,
        )
        if not cached:
            self._schedule_artwork_fetch(info)
        return info

    def _schedule_artwork_fetch(self, info: MediaInfo) -> None:
        if self._loop is None or self._halt.is_set():
            return
        cache_key = self._art.make_key(info.artist, info.title)
        if cache_key in self._pending_art_keys:
            return
        self._pending_art_keys.add(cache_key)

        async def _worker() -> None:
            try:
                url = await self._art.fetch_async(info.artist, info.title, info.album, self._loop)
                if self._halt.is_set():
                    return
                if url:
                    self.message.emit("info", f"album cover resolved ({TARGET_ARTWORK_SIZE}): {info.artist} - {info.title}".strip(" -"))
                    # If the same track is still playing, immediately publish the enriched MediaInfo
                    if self._last_identity is not None and self._last_identity[:4] == info.track_key:
                        enriched = replace(info, artwork_url=url, start_at=self._last_start)
                        self._publish(enriched)
                    self._poke()
                else:
                    self.message.emit("info", f"no online cover found for '{info.title}', using fallback icon")
            except Exception as exc:
                self.message.emit("warn", f"album cover lookup failed: {exc!r}")
            finally:
                self._pending_art_keys.discard(cache_key)

        task = self._loop.create_task(_worker())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _publish(self, info: MediaInfo | None) -> None:
        if info is None:
            if self._last_identity is not None:
                self._last_identity, self._last_start = None, None
                self.media_changed.emit(None)
            return
        changed = info.identity != self._last_identity
        sought = (
            info.start_at is not None
            and self._last_start is not None
            and abs(info.start_at - self._last_start) > SEEK_TOLERANCE_SECONDS
        )
        if changed or sought:
            self._last_identity, self._last_start = info.identity, info.start_at
            self.media_changed.emit(info)

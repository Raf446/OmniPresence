"""Windows System Media Transport Controls listener with high-accuracy, non-blocking Album Art fetcher."""
from __future__ import annotations

import asyncio
import difflib
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Signal

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

POLL_FALLBACK_SECONDS = 0.8
SEEK_TOLERANCE_SECONDS = 3.0

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
DEEZER_SEARCH_URL = "https://api.deezer.com/search"
HTTP_TIMEOUT_SECONDS = 1.8
TARGET_ARTWORK_SIZE = "600x600"
MIN_MATCH_SCORE = 0.58
USER_AGENT = "OmniPresence/1.0 (+https://github.com/Raf446/OmniPresence)"

# Bracketed noise patterns: (...), [...], {...} containing feat/official/remaster/from movie/live/etc.
_BRACKET_NOISE_RE = re.compile(
    r"[\(\[\{][^\)\]\}]*\b("
    r"feat\.?|ft\.?|featuring|with|"
    r"official\s*(?:music\s*)?(?:video|audio|lyric\s*video|visualizer)?|"
    r"lyric\s*video|lyrics?|audio|video|visualizer|mv|hd|4k|hq|"
    r"remaster(?:ed)?|anniversary|deluxe|expanded|special\s*edition|bonus\s*track|"
    r"from\s+[\"'“”]?.*|motion\s*picture|soundtrack|ost|original\s*score|"
    r"live(?:\s+at\b|\s+from\b|\s+in\b|\s+version\b)?|acoustic|unplugged|radio\s*edit|"
    r"explicit|clean|version|edit|mono|stereo|slowed|reverb|sped\s*up|nightcore"
    r")\b[^\)\]\}]*[\)\]\}]",
    re.IGNORECASE,
)

# Trailing dash/pipe suffixes: "- Remastered 2011", "- From 'Movie'", "- Live", "- Official Video", etc.
_DASH_SUFFIX_RE = re.compile(
    r"\s+[-–—|]\s+("
    r"(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?|"
    r"from\s+[\"'“”]?.*|"
    r"live(?:\s+at\b|\s+from\b|\s+in\b|\s+version\b.*)?|"
    r"official\s*(?:music\s*)?(?:video|audio|lyrics?|visualizer).*|"
    r"radio\s*edit|acoustic(?:\s+version)?|single\s+version|album\s+version|"
    r"bonus\s*track|explicit|clean|slowed.*|sped\s*up.*|"
    r"youtube(?:\s*music)?|spotify|vlc\s*media\s*player"
    r")$",
    re.IGNORECASE,
)

# Inline featuring suffix outside brackets: "Song Title feat. Someone" / "Song Title ft. Someone"
_INLINE_FEAT_RE = re.compile(r"\s+\b(?:feat\.?|ft\.?|featuring)\s+.+$", re.IGNORECASE)

# Split multiple artists: "Artist A, Artist B & Artist C" -> primary artist "Artist A"
_ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|;|&|/|\bx\b|\bfeat\.?\b|\bft\.?\b|\bwith\b)\s*", re.IGNORECASE)

# Unwanted tribute / karaoke candidates when original query is not karaoke
_TRIBUTE_NOISE_RE = re.compile(
    r"\b(karaoke|tribute|in\s+the\s+style\s+of|made\s+famous\s+by|8-bit|lullaby\s+version|ringtone|parody)\b",
    re.IGNORECASE,
)

_ITUNES_DIM_RE = re.compile(r"/\d+x\d+(bb|sr|bf|cc)?\.(jpg|jpeg|png|webp)$", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^\w\s]", re.UNICODE)
_MULTI_SPACE_RE = re.compile(r"\s+")


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def sanitize_title(title: str) -> str:
    """Clean track title by removing bracketed noise (feat, From Movie, Remastered, Live, Official Video) and suffixes."""
    cleaned = (title or "").strip()
    if not cleaned:
        return ""
    # Repeatedly strip noisy bracketed segments
    for _ in range(3):
        next_val = _BRACKET_NOISE_RE.sub(" ", cleaned)
        if next_val == cleaned:
            break
        cleaned = next_val
    cleaned = _DASH_SUFFIX_RE.sub("", cleaned)
    cleaned = _INLINE_FEAT_RE.sub("", cleaned)
    cleaned = _MULTI_SPACE_RE.sub(" ", cleaned).strip(" -–—|•")
    return cleaned or title.strip()


def sanitize_artist(artist: str) -> str:
    """Extract clean primary artist name without featuring/collaborator clutter."""
    cleaned = (artist or "").strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"\s*-\s*Topic$", "", cleaned, flags=re.IGNORECASE)
    cleaned = _BRACKET_NOISE_RE.sub(" ", cleaned)
    parts = [p.strip() for p in _ARTIST_SPLIT_RE.split(cleaned) if p.strip()]
    primary = parts[0] if parts else cleaned
    return _MULTI_SPACE_RE.sub(" ", primary).strip(" -–—|•")


def normalize_for_comparison(text: str) -> str:
    """Lowercase, accent-folded, punctuation-free single-space string for similarity scoring."""
    folded = _strip_accents((text or "").lower())
    alnum = _NON_ALNUM_RE.sub(" ", folded)
    return _MULTI_SPACE_RE.sub(" ", alnum).strip()


def normalize_cache_key(artist: str, title: str) -> str:
    """Normalized lowercase single-space key so minor title/artist variations hit the same cache entry."""
    norm_artist = normalize_for_comparison(sanitize_artist(artist) or artist)
    norm_title = normalize_for_comparison(sanitize_title(title) or title)
    return f"{norm_artist} - {norm_title}".strip()


def _string_similarity(a: str, b: str) -> float:
    na, nb = normalize_for_comparison(a), normalize_for_comparison(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    seq_ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    tokens_a, tokens_b = set(na.split()), set(nb.split())
    if tokens_a and tokens_b:
        intersection = len(tokens_a & tokens_b)
        token_Coverage = intersection / max(min(len(tokens_a), len(tokens_b)), 1)
        jaccard = intersection / len(tokens_a | tokens_b)
        token_score = 0.6 * token_Coverage + 0.4 * jaccard
    else:
        token_score = 0.0
    return max(seq_ratio, 0.55 * seq_ratio + 0.45 * token_score)


def score_candidate(
    target_artist: str,
    target_title: str,
    cand_artist: str,
    cand_title: str,
) -> float:
    """Compute a [0.0..1.0] confidence score comparing candidate metadata against the target track."""
    clean_t_title = sanitize_title(target_title)
    clean_c_title = sanitize_title(cand_title)
    title_sim = max(
        _string_similarity(clean_t_title, clean_c_title),
        _string_similarity(target_title, cand_title),
    )

    if target_artist.strip():
        clean_t_artist = sanitize_artist(target_artist)
        clean_c_artist = sanitize_artist(cand_artist)
        artist_sim = max(
            _string_similarity(clean_t_artist, clean_c_artist),
            _string_similarity(target_artist, cand_artist),
        )
        # Hard guard: if either title or artist is completely mismatched, penalize heavily
        if title_sim < 0.45 or artist_sim < 0.40:
            combined = min(title_sim, artist_sim) * 0.65
        else:
            combined = 0.55 * title_sim + 0.45 * artist_sim
    else:
        combined = title_sim * 0.85

    # Penalize tribute/karaoke covers unless the user is actually listening to a karaoke/tribute track
    wanted_tribute = bool(_TRIBUTE_NOISE_RE.search(target_title) or _TRIBUTE_NOISE_RE.search(target_artist))
    cand_is_tribute = bool(_TRIBUTE_NOISE_RE.search(cand_title) or _TRIBUTE_NOISE_RE.search(cand_artist))
    if cand_is_tribute and not wanted_tribute:
        combined -= 0.35

    return max(0.0, min(1.0, combined))


def upgrade_artwork_resolution(url: str, size: str = TARGET_ARTWORK_SIZE) -> str:
    """Upgrade iTunes / Deezer thumbnail URLs to high resolution (default 600x600)."""
    url = url.strip()
    if not url.lower().startswith("https://"):
        return ""
    if "mzstatic.com" in url:
        return _ITUNES_DIM_RE.sub(rf"/{size}bb.\2", url)
    if "dzcdn.net" in url:
        return re.sub(r"/\d+x\d+-", f"/{size}-", url)
    return url


class AlbumArtResolver:
    """Thread-safe in-memory LRU cache and rate-limited public API resolver with metadata validation scoring."""

    def __init__(
        self,
        max_entries: int = 256,
        miss_ttl_seconds: float = 90.0,
        min_request_gap: float = 0.25,
    ) -> None:
        self._max_entries = max_entries
        self._miss_ttl = miss_ttl_seconds
        self._min_gap = min_request_gap
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, tuple[str, float, bool]] = OrderedDict()
        self._last_request_at = 0.0

    @staticmethod
    def make_key(artist: str, title: str) -> str:
        return normalize_cache_key(artist, title)

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
        """Lookup album cover URL with query sanitization, candidate scoring, and strict 1.8s timeout."""
        if not (title or "").strip():
            return ""
        cached, url = self.get_cached(artist, title)
        if cached:
            return url

        clean_title = sanitize_title(title)
        clean_artist = sanitize_artist(artist)

        search_terms: list[str] = []
        if clean_artist and clean_title:
            search_terms.append(f"{clean_artist} {clean_title}")
        if artist.strip() and clean_title:
            raw_combo = f"{artist.strip()} {clean_title}"
            if raw_combo not in search_terms:
                search_terms.append(raw_combo)
        if clean_title and clean_title not in search_terms:
            search_terms.append(clean_title)

        best_url = ""
        best_score = 0.0

        for term in search_terms:
            itunes_url, itunes_score = self._query_itunes(term, artist, title)
            if itunes_score > best_score:
                best_score, best_url = itunes_score, itunes_url
            if best_score >= 0.78:
                break

            deezer_url, deezer_score = self._query_deezer(term, artist, title)
            if deezer_score > best_score:
                best_score, best_url = deezer_score, deezer_url
            if best_score >= 0.78:
                break

        final_url = best_url if best_score >= MIN_MATCH_SCORE else ""
        self._store(artist, title, final_url)
        return final_url

    async def fetch_async(
        self,
        artist: str,
        title: str,
        album: str = "",
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> str:
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

    def _query_itunes(self, term: str, target_artist: str, target_title: str) -> tuple[str, float]:
        # Format: https://itunes.apple.com/search?term={clean_artist}+{clean_title}&entity=song&limit=5
        encoded_term = urllib.parse.quote_plus(term.strip())
        url = f"{ITUNES_SEARCH_URL}?term={encoded_term}&entity=song&limit=5"
        payload = self._http_json(url)
        if not payload:
            return "", 0.0
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            return "", 0.0

        best_url = ""
        best_score = 0.0
        for item in results[:5]:
            if not isinstance(item, dict):
                continue
            raw_art = str(item.get("artworkUrl100") or item.get("artworkUrl60") or "").strip()
            if not raw_art:
                continue
            cand_title = str(item.get("trackName") or "")
            cand_artist = str(item.get("artistName") or "")
            score = score_candidate(target_artist, target_title, cand_artist, cand_title)
            if score > best_score:
                best_score = score
                best_url = raw_art

        if best_url and best_score >= MIN_MATCH_SCORE:
            return upgrade_artwork_resolution(best_url, TARGET_ARTWORK_SIZE), best_score
        return "", best_score

    def _query_deezer(self, term: str, target_artist: str, target_title: str) -> tuple[str, float]:
        encoded_term = urllib.parse.quote_plus(term.strip())
        url = f"{DEEZER_SEARCH_URL}?q={encoded_term}&limit=5"
        payload = self._http_json(url)
        if not payload:
            return "", 0.0
        results = payload.get("data")
        if not isinstance(results, list) or not results:
            return "", 0.0

        best_url = ""
        best_score = 0.0
        for item in results[:5]:
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
            cand_title = str(item.get("title") or "")
            cand_artist = str(artist_obj.get("name") or "")
            score = score_candidate(target_artist, target_title, cand_artist, cand_title)
            if score > best_score:
                best_score = score
                best_url = raw_art

        if best_url and best_score >= MIN_MATCH_SCORE:
            return upgrade_artwork_resolution(best_url, TARGET_ARTWORK_SIZE), best_score
        return "", best_score


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


class _ArtworkSignals(QObject):
    finished = Signal(str, object, str)


class _ArtworkRunnable(QRunnable):
    """QThreadPool background task that resolves album cover URL without blocking SMTC or IPC."""

    def __init__(self, resolver: AlbumArtResolver, cache_key: str, info: MediaInfo) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._resolver = resolver
        self._cache_key = cache_key
        self._info = info
        self.signals = _ArtworkSignals()

    def run(self) -> None:
        url = ""
        try:
            url = self._resolver.fetch_sync(self._info.artist, self._info.title, self._info.album)
        except Exception:
            url = ""
        try:
            self.signals.finished.emit(self._cache_key, self._info, url)
        except RuntimeError:
            pass


class MediaScrobbler(QThread):
    """Runs an asyncio loop that watches SMTC sessions and dispatches QThreadPool tasks for album art."""

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
        self._pool = QThreadPool.globalInstance()
        self._halt = threading.Event()
        self._state_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._hooked: set[str] = set()
        self._handlers: list = []
        self._last_identity: tuple | None = None
        self._last_start: float | None = None
        self._pending_art_keys: set[str] = set()
        self._active_runnables: set[_ArtworkRunnable] = set()

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
            self._loop = None
            loop.close()

    async def _watch(self) -> None:
        self._wake = asyncio.Event()
        manager = await SessionManager.request_async()
        self._subscribe_manager(manager)
        self.message.emit("info", "listening for Windows media sessions (instant event trigger + validated album art)")
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
            if loop is not None and wake is not None and not loop.is_closed():
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
            if loop is not None and wake is not None and not loop.is_closed():
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

        # Instant check against normalized LRU cache; if missing, emit placeholder immediately and dispatch QThreadPool task
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
            self._dispatch_artwork_task(info)
        return info

    def _dispatch_artwork_task(self, info: MediaInfo) -> None:
        if self._halt.is_set():
            return
        cache_key = self._art.make_key(info.artist, info.title)
        with self._state_lock:
            if cache_key in self._pending_art_keys:
                return
            self._pending_art_keys.add(cache_key)

        runnable = _ArtworkRunnable(self._art, cache_key, info)
        self._active_runnables.add(runnable)

        def _on_done(key: str, orig_info: MediaInfo, url: str, r=runnable) -> None:
            self._active_runnables.discard(r)
            with self._state_lock:
                self._pending_art_keys.discard(key)
                current_id = self._last_identity
                current_start = self._last_start
            if self._halt.is_set():
                return
            if url:
                clean_label = f"{sanitize_artist(orig_info.artist)} - {sanitize_title(orig_info.title)}".strip(" -")
                self.message.emit("info", f"album cover matched ({TARGET_ARTWORK_SIZE}): {clean_label}")
                if current_id is not None and current_id[:4] == orig_info.track_key:
                    enriched = replace(orig_info, artwork_url=url, start_at=current_start)
                    self._publish(enriched)
                self._poke()
            else:
                self.message.emit(
                    "info",
                    f"cover score below threshold or unavailable for '{orig_info.title}', using fallback icon",
                )

        runnable.signals.finished.connect(_on_done)
        self._pool.start(runnable)

    def _publish(self, info: MediaInfo | None) -> None:
        with self._state_lock:
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

"""Windows System Media Transport Controls listener with high-accuracy, non-blocking Album Art fetcher."""
from __future__ import annotations

import asyncio
import difflib
import hashlib
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
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThread, QThreadPool, QTimer, Signal, Slot

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
MEDIA_STOP_GRACE_PERIOD = 1.5

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
DEEZER_SEARCH_URL = "https://api.deezer.com/search"
DEEZER_ALBUM_SEARCH_URL = "https://api.deezer.com/search/album"
HTTP_TIMEOUT_SECONDS = 2.0
TARGET_ARTWORK_SIZE = "600x600"
MIN_MATCH_SCORE = 0.46
USER_AGENT = "OmniPresence/2.3.1 (+https://github.com/Raf446/OmniPresence)"

DEFAULT_FALLBACK_ICON = "music_placeholder"

# Bracketed noise patterns: (...), [...], {...} containing feat/official/remaster/from movie/live/etc.
_BRACKET_NOISE_RE = re.compile(
    r"[\(\[\{（【][^\)\]\｝）】]*\b("
    r"feat\.?|ft\.?|featuring|with|prod\.?\s*by|"
    r"official\s*(?:music\s*)?(?:video|audio|lyric\s*video|visualizer)?|"
    r"lyric\s*video|lyrics?|audio|video|visualizer|mv|pv|hd|4k|hq|"
    r"remaster(?:ed)?|anniversary|deluxe|expanded|special\s*edition|bonus\s*track|"
    r"from\s+[\"'“”]?.*|motion\s*picture|soundtrack|ost|original\s*score|"
    r"live(?:\s+at\b|\s+from\b|\s+in\b|\s+version\b)?|acoustic|unplugged|radio\s*edit|"
    r"explicit|clean|version|ver\.?|edit|mono|stereo|slowed|reverb|sped\s*up|nightcore|"
    r"inst\.?|instrumental|short\s*ver\.?|tv\s*size"
    r")\b[^\)\]\｝）】]*[\)\]\｝）】]",
    re.IGNORECASE,
)

# Any remaining parenthetical/bracketed segments: (...), [...], {...}, （...）, 【...】
_ANY_BRACKET_RE = re.compile(r"\s*[\(\[\{（【][^\)\]\｝）】]*[\)\]\｝）】]")

# Trailing dash/pipe/slash suffixes: "- Remastered 2011", "- From 'Movie'", "- Live", "- Official Video", "- Single", etc.
_DASH_SUFFIX_RE = re.compile(
    r"\s+[-–—|/]\s+("
    r"(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?.*|"
    r"from\s+[\"'“”]?.*|"
    r"live(?:\s+at\b|\s+from\b|\s+in\b|\s+version\b.*)?|"
    r"official\s*(?:music\s*)?(?:video|audio|lyrics?|visualizer).*|"
    r"radio\s*edit|acoustic(?:\s+version)?|single(?:\s+version)?|album\s+version|ep\b.*|"
    r"deluxe(?:\s+edition|\s+version)?.*|expanded(?:\s+edition)?.*|anniversary(?:\s+edition)?.*|"
    r"bonus\s*track|explicit|clean|slowed.*|sped\s*up.*|instrumental.*|"
    r"(?:feat\.?|ft\.?|featuring|with)\s+.*|"
    r"youtube(?:\s*music)?|spotify|vlc\s*media\s*player"
    r")$",
    re.IGNORECASE,
)

# Inline featuring / producer suffix outside brackets: "Song Title feat. Someone" / "Song Title ft. Someone"
_INLINE_FEAT_RE = re.compile(
    r"\s+\b(?:feat\.?|ft\.?|featuring|prod\.?\s*by)\b\s+.+$",
    re.IGNORECASE,
)

# Split multiple artists: "Artist A, Artist B & Artist C" -> primary artist "Artist A"
_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|;|&|/|\bx\b|\b×\b|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bwith\b)\s*",
    re.IGNORECASE,
)

# Unwanted tribute / karaoke candidates when original query is not karaoke
_TRIBUTE_NOISE_RE = re.compile(
    r"\b(karaoke|tribute|in\s+the\s+style\s+of|made\s+famous\s+by|8-bit|lullaby\s+version|ringtone|parody|cover\s+version)\b",
    re.IGNORECASE,
)

_ITUNES_DIM_RE = re.compile(r"/\d+x\d+(bb|sr|bf|cc)?\.(jpg|jpeg|png|webp)$", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^\w\s]", re.UNICODE)
_MULTI_SPACE_RE = re.compile(r"\s+")


def fallback_artwork_for_app(app_id: str) -> str:
    """Return the appropriate Discord asset key fallback when no external cover URL is available."""
    lowered = (app_id or "").lower()
    if "spotify" in lowered:
        return "spotify"
    if "apple" in lowered:
        return "applemusic"
    return DEFAULT_FALLBACK_ICON


def make_track_hash(artist: str, title: str) -> str:
    """Create a deterministic unique hash string for (artist + title) to identify active SMTC tracks."""
    norm_a = (artist or "").strip().lower()
    norm_t = (title or "").strip().lower()
    raw = f"{norm_a}::{norm_t}".encode("utf-8", errors="replace")
    return hashlib.sha1(raw).hexdigest()[:16]


def _strip_accents(text: str) -> str:
    """Fold Latin diacritics while preserving CJK/Kanji, Hiragana/Katakana dakuten, and Hangul characters."""
    if not text:
        return ""
    kc = unicodedata.normalize("NFKC", text)
    decomposed = unicodedata.normalize("NFD", kc)
    # Strip only Latin/general combining diacritical marks (U+0300..U+036F), keeping CJK/Kana voicing marks (U+3099/U+309A)
    filtered = "".join(ch for ch in decomposed if not (0x0300 <= ord(ch) <= 0x036F))
    return unicodedata.normalize("NFC", filtered)


def sanitize_title(title: str) -> str:
    """Clean track title by removing bracketed noise (...), [...], feat/with clauses, and remaster/video suffixes."""
    cleaned = (title or "").strip()
    if not cleaned:
        return ""
    cleaned = unicodedata.normalize("NFKC", cleaned)

    # Step 1: Strip known noisy bracketed segments
    for _ in range(3):
        next_val = _BRACKET_NOISE_RE.sub(" ", cleaned)
        if next_val == cleaned:
            break
        cleaned = next_val

    # Step 2: Strip remaining (...) and [...] segments unless the entire title was inside brackets
    stripped_all_brackets = _ANY_BRACKET_RE.sub(" ", cleaned).strip(" -–—|•/·")
    if len(stripped_all_brackets) >= 2:
        cleaned = stripped_all_brackets

    # Step 3: Strip trailing dash suffixes and inline feat./with collaborators
    cleaned = _DASH_SUFFIX_RE.sub("", cleaned)
    cleaned = _INLINE_FEAT_RE.sub("", cleaned)
    cleaned = _MULTI_SPACE_RE.sub(" ", cleaned).strip(" -–—|•/·「」『』")
    return cleaned or unicodedata.normalize("NFKC", title.strip())


def sanitize_artist(artist: str) -> str:
    """Extract clean primary artist name without featuring/collaborator clutter."""
    cleaned = (artist or "").strip()
    if not cleaned:
        return ""
    cleaned = unicodedata.normalize("NFKC", cleaned)
    cleaned = re.sub(r"\s*-\s*Topic$", "", cleaned, flags=re.IGNORECASE)
    cleaned = _BRACKET_NOISE_RE.sub(" ", cleaned)
    stripped_brackets = _ANY_BRACKET_RE.sub(" ", cleaned).strip()
    if len(stripped_brackets) >= 2:
        cleaned = stripped_brackets
    parts = [p.strip() for p in _ARTIST_SPLIT_RE.split(cleaned) if p.strip()]
    primary = parts[0] if parts else cleaned
    return _MULTI_SPACE_RE.sub(" ", primary).strip(" -–—|•/·")


def sanitize_album(album: str) -> str:
    """Clean album title by removing (Deluxe Edition), [Remastered], - Single, - EP, and bracketed noise."""
    cleaned = (album or "").strip()
    if not cleaned:
        return ""
    cleaned = unicodedata.normalize("NFKC", cleaned)
    for _ in range(3):
        next_val = _BRACKET_NOISE_RE.sub(" ", cleaned)
        if next_val == cleaned:
            break
        cleaned = next_val
    stripped_brackets = _ANY_BRACKET_RE.sub(" ", cleaned).strip(" -–—|•/·")
    if len(stripped_brackets) >= 2:
        cleaned = stripped_brackets
    cleaned = _DASH_SUFFIX_RE.sub("", cleaned)
    cleaned = _INLINE_FEAT_RE.sub("", cleaned)
    cleaned = _MULTI_SPACE_RE.sub(" ", cleaned).strip(" -–—|•/·「」『』")
    return cleaned or unicodedata.normalize("NFKC", album.strip())


def _extract_artist_tokens(artist: str) -> list[str]:
    """Return all individual collaborator/artist names from a multi-artist string."""
    cleaned = unicodedata.normalize("NFKC", (artist or "").strip())
    if not cleaned:
        return []
    cleaned = re.sub(r"\s*-\s*Topic$", "", cleaned, flags=re.IGNORECASE)
    cleaned = _BRACKET_NOISE_RE.sub(" ", cleaned)
    parts = [p.strip(" -–—|•/·") for p in _ARTIST_SPLIT_RE.split(cleaned) if p.strip(" -–—|•/·")]
    return parts if parts else [cleaned]


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

    # Substring containment boost for titles/artists with localized subtitles or extra descriptors
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    containment_score = 0.0
    if len(shorter) >= 2 and shorter in longer:
        length_ratio = len(shorter) / max(len(longer), 1)
        containment_score = 0.72 + 0.28 * length_ratio

    tokens_a, tokens_b = set(na.split()), set(nb.split())
    if tokens_a and tokens_b:
        intersection = len(tokens_a & tokens_b)
        token_coverage = intersection / max(min(len(tokens_a), len(tokens_b)), 1)
        jaccard = intersection / len(tokens_a | tokens_b)
        token_score = 0.65 * token_coverage + 0.35 * jaccard
    else:
        token_score = 0.0

    return max(seq_ratio, containment_score, 0.50 * seq_ratio + 0.50 * token_score)


def score_candidate(
    target_artist: str,
    target_title: str,
    cand_artist: str,
    cand_title: str,
    target_album: str = "",
    cand_album: str = "",
) -> float:
    """Compute a [0.0..1.0] confidence score comparing candidate metadata against the target track."""
    clean_t_title = sanitize_title(target_title)
    clean_c_title = sanitize_title(cand_title)
    title_sim = max(
        _string_similarity(clean_t_title, clean_c_title),
        _string_similarity(target_title, cand_title),
    )

    album_sim = 0.0
    if target_album.strip() and cand_album.strip():
        clean_t_album = sanitize_album(target_album)
        clean_c_album = sanitize_album(cand_album)
        album_sim = max(
            _string_similarity(clean_t_album, clean_c_album),
            _string_similarity(target_album, cand_album),
        )

    if target_artist.strip():
        clean_t_artist = sanitize_artist(target_artist)
        clean_c_artist = sanitize_artist(cand_artist)
        artist_sim = max(
            _string_similarity(clean_t_artist, clean_c_artist),
            _string_similarity(target_artist, cand_artist),
        )
        # Also check all collaborator names in case SMTC lists multiple artists or reversed order
        for sub_artist in _extract_artist_tokens(target_artist):
            for cand_sub in _extract_artist_tokens(cand_artist):
                artist_sim = max(artist_sim, _string_similarity(sub_artist, cand_sub))

        if title_sim >= 0.40 and artist_sim >= 0.35:
            combined = 0.56 * title_sim + 0.44 * artist_sim
        elif title_sim >= 0.72:
            # Handle CJK/Kanji vs Romanized artist name mismatch when track title matches strongly
            combined = title_sim * 0.74 + artist_sim * 0.20
        elif artist_sim >= 0.75 and title_sim >= 0.30:
            # Same artist with heavily stylized/localized track title
            combined = 0.48 * title_sim + 0.52 * artist_sim
        else:
            combined = 0.52 * title_sim + 0.48 * artist_sim
            if title_sim < 0.28 and artist_sim < 0.28:
                combined *= 0.65
    else:
        combined = title_sim * 0.88

    if album_sim >= 0.55:
        combined = max(combined, 0.45 * title_sim + 0.30 * album_sim + 0.25 * (artist_sim if target_artist.strip() else title_sim))
        combined += 0.06 * album_sim

    # Penalize tribute/karaoke covers unless the user is actually listening to a karaoke/tribute track
    wanted_tribute = bool(_TRIBUTE_NOISE_RE.search(target_title) or _TRIBUTE_NOISE_RE.search(target_artist))
    cand_is_tribute = bool(_TRIBUTE_NOISE_RE.search(cand_title) or _TRIBUTE_NOISE_RE.search(cand_artist))
    if cand_is_tribute and not wanted_tribute:
        combined -= 0.35

    return max(0.0, min(1.0, combined))


def score_album_candidate(
    target_artist: str,
    target_album: str,
    cand_artist: str,
    cand_album: str,
) -> float:
    """Compute confidence score for secondary fallback search based on (Artist + Album)."""
    clean_t_album = sanitize_album(target_album)
    clean_c_album = sanitize_album(cand_album)
    album_sim = max(
        _string_similarity(clean_t_album, clean_c_album),
        _string_similarity(target_album, cand_album),
    )
    if not target_artist.strip():
        return album_sim * 0.86

    clean_t_artist = sanitize_artist(target_artist)
    clean_c_artist = sanitize_artist(cand_artist)
    artist_sim = max(
        _string_similarity(clean_t_artist, clean_c_artist),
        _string_similarity(target_artist, cand_artist),
    )
    for sub_artist in _extract_artist_tokens(target_artist):
        for cand_sub in _extract_artist_tokens(cand_artist):
            artist_sim = max(artist_sim, _string_similarity(sub_artist, cand_sub))

    if album_sim >= 0.42 and artist_sim >= 0.35:
        combined = 0.56 * album_sim + 0.44 * artist_sim
    elif album_sim >= 0.78:
        combined = album_sim * 0.72 + artist_sim * 0.20
    else:
        combined = min(album_sim, artist_sim) * 0.75

    wanted_tribute = bool(_TRIBUTE_NOISE_RE.search(target_album) or _TRIBUTE_NOISE_RE.search(target_artist))
    cand_is_tribute = bool(_TRIBUTE_NOISE_RE.search(cand_album) or _TRIBUTE_NOISE_RE.search(cand_artist))
    if cand_is_tribute and not wanted_tribute:
        combined -= 0.35

    return max(0.0, min(1.0, combined))


def sanitize_artwork_url(url: str, size: str = TARGET_ARTWORK_SIZE) -> str:
    """Validate and normalize artwork URL to strict HTTPS and upgrade resolution to 600x600."""
    cleaned = (url or "").strip()
    if not cleaned:
        return ""
    if cleaned.startswith("//"):
        cleaned = "https:" + cleaned
    elif cleaned.lower().startswith("http://"):
        cleaned = "https://" + cleaned[7:]
    if not cleaned.lower().startswith("https://"):
        return ""
    if "mzstatic.com" in cleaned:
        cleaned = _ITUNES_DIM_RE.sub(rf"/{size}bb.\2", cleaned)
    elif "dzcdn.net" in cleaned:
        cleaned = re.sub(r"/\d+x\d+-", f"/{size}-", cleaned)
    try:
        parsed = urllib.parse.urlsplit(cleaned)
        if parsed.scheme.lower() != "https" or not parsed.netloc or "." not in parsed.netloc:
            return ""
        if any(ch.isspace() for ch in cleaned) or len(cleaned) > 256:
            return ""
    except Exception:
        return ""
    return cleaned


def upgrade_artwork_resolution(url: str, size: str = TARGET_ARTWORK_SIZE) -> str:
    """Upgrade iTunes / Deezer thumbnail URLs to high resolution (default 600x600) and enforce HTTPS."""
    return sanitize_artwork_url(url, size=size)


class AlbumArtResolver:
    """Thread-safe in-memory LRU cache and rate-limited public API resolver with metadata validation scoring."""

    def __init__(
        self,
        max_entries: int = 256,
        miss_ttl_seconds: float = 45.0,
        min_request_gap: float = 0.18,
    ) -> None:
        self._max_entries = max_entries
        self._miss_ttl = miss_ttl_seconds
        self._min_gap = min_request_gap
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, tuple[str, float, bool]] = OrderedDict()
        self._last_request_at = 0.0

    @staticmethod
    def make_key(artist: str, title: str, album: str = "") -> str:
        base = normalize_cache_key(artist, title)
        if album.strip():
            norm_alb = normalize_for_comparison(sanitize_album(album) or album)
            if norm_alb:
                return f"{base} | {norm_alb}".strip()
        return base

    def get_cached(self, artist: str, title: str, album: str = "") -> tuple[bool, str]:
        """Return (found_in_cache, artwork_url). Negative entries expire after miss_ttl_seconds."""
        keys_to_check = [self.make_key(artist, title, album)]
        base_key = self.make_key(artist, title)
        if base_key not in keys_to_check:
            keys_to_check.append(base_key)

        now = time.monotonic()
        with self._lock:
            for key in keys_to_check:
                entry = self._cache.get(key)
                if entry is None:
                    continue
                url, ts, is_hit = entry
                if not is_hit and (now - ts) >= self._miss_ttl:
                    del self._cache[key]
                    continue
                self._cache.move_to_end(key)
                return True, url
        return False, ""

    def _store(self, artist: str, title: str, url: str, album: str = "") -> None:
        clean_url = sanitize_artwork_url(url)
        full_key = self.make_key(artist, title, album)
        base_key = self.make_key(artist, title)
        now = time.monotonic()
        with self._lock:
            self._cache[full_key] = (clean_url, now, bool(clean_url))
            self._cache.move_to_end(full_key)
            if clean_url or base_key not in self._cache:
                self._cache[base_key] = (clean_url, now, bool(clean_url))
                self._cache.move_to_end(base_key)
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)

    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._min_gap - (now - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def fetch_sync(
        self,
        artist: str,
        title: str,
        album: str = "",
        is_stale: Callable[[], bool] | None = None,
    ) -> str:
        """Lookup album cover URL with query sanitization, candidate scoring, and Artist+Album fallback."""
        if not (title or "").strip() and not (album or "").strip():
            return ""
        cached, url = self.get_cached(artist, title, album)
        if cached:
            return url

        clean_title = sanitize_title(title)
        clean_artist = sanitize_artist(artist)
        clean_album = sanitize_album(album)

        search_terms: list[str] = []
        if clean_artist and clean_title:
            search_terms.append(f"{clean_artist} {clean_title}")
        if artist.strip() and clean_title:
            raw_combo = f"{artist.strip()} {clean_title}"
            if raw_combo not in search_terms:
                search_terms.append(raw_combo)
        if clean_title and clean_title not in search_terms:
            search_terms.append(clean_title)
        if title.strip() and title.strip() not in search_terms:
            search_terms.append(title.strip())

        best_url = ""
        best_score = 0.0

        # Primary Search: Track Title (+ Artist) across iTunes & Deezer
        for term in search_terms[:3]:
            if is_stale is not None and is_stale():
                return ""
            itunes_url, itunes_score = self._query_itunes(term, artist, title, album)
            if itunes_score > best_score and itunes_url:
                best_score, best_url = itunes_score, itunes_url
            if best_score >= 0.72 and best_url:
                break

            if is_stale is not None and is_stale():
                return ""
            deezer_url, deezer_score = self._query_deezer(term, artist, title, album)
            if deezer_score > best_score and deezer_url:
                best_score, best_url = deezer_score, deezer_url
            if best_score >= 0.72 and best_url:
                break

        # Secondary Fallback Search: Artist + Album when Title search yields low score or no match
        if (best_score < MIN_MATCH_SCORE or not best_url) and clean_album:
            album_terms: list[str] = []
            if clean_artist:
                album_terms.append(f"{clean_artist} {clean_album}")
            if artist.strip() and f"{artist.strip()} {clean_album}" not in album_terms:
                album_terms.append(f"{artist.strip()} {clean_album}")
            if clean_album not in album_terms:
                album_terms.append(clean_album)

            for a_term in album_terms[:2]:
                if is_stale is not None and is_stale():
                    return ""
                alb_it_url, alb_it_score = self._query_itunes_album(a_term, artist, album)
                if alb_it_score > best_score and alb_it_url:
                    best_score, best_url = alb_it_score, alb_it_url
                if best_score >= 0.70 and best_url:
                    break

                if is_stale is not None and is_stale():
                    return ""
                alb_dz_url, alb_dz_score = self._query_deezer_album(a_term, artist, album)
                if alb_dz_score > best_score and alb_dz_url:
                    best_score, best_url = alb_dz_score, alb_dz_url
                if best_score >= 0.70 and best_url:
                    break

        if is_stale is not None and is_stale():
            return ""

        final_url = sanitize_artwork_url(best_url) if best_score >= MIN_MATCH_SCORE else ""
        self._store(artist, title, final_url, album)
        return final_url

    async def fetch_async(
        self,
        artist: str,
        title: str,
        album: str = "",
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> str:
        cached, url = self.get_cached(artist, title, album)
        if cached:
            return url
        active_loop = loop or asyncio.get_running_loop()
        return await active_loop.run_in_executor(None, self.fetch_sync, artist, title, album, None)

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

    def _query_itunes(
        self,
        term: str,
        target_artist: str,
        target_title: str,
        target_album: str = "",
    ) -> tuple[str, float]:
        encoded_term = urllib.parse.quote_plus(term.strip())
        url = f"{ITUNES_SEARCH_URL}?term={encoded_term}&entity=song&limit=8"
        payload = self._http_json(url)
        if not payload:
            return "", 0.0
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            return "", 0.0

        best_url = ""
        best_score = 0.0
        for item in results[:8]:
            if not isinstance(item, dict):
                continue
            raw_art = str(item.get("artworkUrl100") or item.get("artworkUrl60") or "").strip()
            clean_art = sanitize_artwork_url(raw_art, TARGET_ARTWORK_SIZE)
            if not clean_art:
                continue
            cand_title = str(item.get("trackName") or "")
            cand_artist = str(item.get("artistName") or "")
            cand_album = str(item.get("collectionName") or "")
            score = score_candidate(
                target_artist,
                target_title,
                cand_artist,
                cand_title,
                target_album=target_album,
                cand_album=cand_album,
            )
            if score > best_score:
                best_score = score
                best_url = clean_art

        if best_url and best_score >= MIN_MATCH_SCORE:
            return best_url, best_score
        return "", best_score

    def _query_itunes_album(
        self,
        term: str,
        target_artist: str,
        target_album: str,
    ) -> tuple[str, float]:
        encoded_term = urllib.parse.quote_plus(term.strip())
        url = f"{ITUNES_SEARCH_URL}?term={encoded_term}&entity=album&limit=6"
        payload = self._http_json(url)
        if not payload:
            return "", 0.0
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            return "", 0.0

        best_url = ""
        best_score = 0.0
        for item in results[:6]:
            if not isinstance(item, dict):
                continue
            raw_art = str(item.get("artworkUrl100") or item.get("artworkUrl60") or "").strip()
            clean_art = sanitize_artwork_url(raw_art, TARGET_ARTWORK_SIZE)
            if not clean_art:
                continue
            cand_album = str(item.get("collectionName") or "")
            cand_artist = str(item.get("artistName") or "")
            score = score_album_candidate(target_artist, target_album, cand_artist, cand_album)
            if score > best_score:
                best_score = score
                best_url = clean_art

        if best_url and best_score >= MIN_MATCH_SCORE:
            return best_url, best_score
        return "", best_score

    def _query_deezer(
        self,
        term: str,
        target_artist: str,
        target_title: str,
        target_album: str = "",
    ) -> tuple[str, float]:
        encoded_term = urllib.parse.quote_plus(term.strip())
        url = f"{DEEZER_SEARCH_URL}?q={encoded_term}&limit=8"
        payload = self._http_json(url)
        if not payload:
            return "", 0.0
        results = payload.get("data")
        if not isinstance(results, list) or not results:
            return "", 0.0

        best_url = ""
        best_score = 0.0
        for item in results[:8]:
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
            clean_art = sanitize_artwork_url(raw_art, TARGET_ARTWORK_SIZE)
            if not clean_art:
                continue
            cand_title = str(item.get("title") or "")
            cand_artist = str(artist_obj.get("name") or "")
            cand_album = str(album_obj.get("title") or "")
            score = score_candidate(
                target_artist,
                target_title,
                cand_artist,
                cand_title,
                target_album=target_album,
                cand_album=cand_album,
            )
            if score > best_score:
                best_score = score
                best_url = clean_art

        if best_url and best_score >= MIN_MATCH_SCORE:
            return best_url, best_score
        return "", best_score

    def _query_deezer_album(
        self,
        term: str,
        target_artist: str,
        target_album: str,
    ) -> tuple[str, float]:
        encoded_term = urllib.parse.quote_plus(term.strip())
        url = f"{DEEZER_ALBUM_SEARCH_URL}?q={encoded_term}&limit=6"
        payload = self._http_json(url)
        if not payload:
            return "", 0.0
        results = payload.get("data")
        if not isinstance(results, list) or not results:
            return "", 0.0

        best_url = ""
        best_score = 0.0
        for item in results[:6]:
            if not isinstance(item, dict):
                continue
            artist_obj = item.get("artist") or {}
            raw_art = str(
                item.get("cover_xl")
                or item.get("cover_big")
                or item.get("cover_medium")
                or ""
            ).strip()
            clean_art = sanitize_artwork_url(raw_art, TARGET_ARTWORK_SIZE)
            if not clean_art:
                continue
            cand_album = str(item.get("title") or "")
            cand_artist = str(artist_obj.get("name") or "")
            score = score_album_candidate(target_artist, target_album, cand_artist, cand_album)
            if score > best_score:
                best_score = score
                best_url = clean_art

        if best_url and best_score >= MIN_MATCH_SCORE:
            return best_url, best_score
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
    request_id: int = 0
    track_hash: str = ""

    @property
    def track_key(self) -> tuple[str, str, str, str]:
        return (self.app_id, self.title, self.artist, self.album)

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        return (self.app_id, self.title, self.artist, self.album, self.artwork_url)

    @property
    def effective_artwork(self) -> str:
        clean = sanitize_artwork_url(self.artwork_url)
        return clean if clean else fallback_artwork_for_app(self.app_id)


class _ArtworkSignals(QObject):
    # (request_id, track_hash, dispatched_at, cache_key, orig_info, artwork_url)
    finished = Signal(int, str, float, str, object, str)


class _ArtworkRunnable(QRunnable):
    """QThreadPool background task that resolves album cover URL with stale-request cancellation."""

    def __init__(
        self,
        resolver: AlbumArtResolver,
        cache_key: str,
        info: MediaInfo,
        request_id: int,
        track_hash: str,
        dispatched_at: float,
        is_stale_cb: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._resolver = resolver
        self._cache_key = cache_key
        self._info = info
        self._request_id = request_id
        self._track_hash = track_hash
        self._dispatched_at = dispatched_at
        self._is_stale_cb = is_stale_cb
        self.signals = _ArtworkSignals()

    def run(self) -> None:
        if self._is_stale_cb is not None and self._is_stale_cb():
            return
        url = ""
        try:
            url = self._resolver.fetch_sync(
                self._info.artist,
                self._info.title,
                self._info.album,
                is_stale=self._is_stale_cb,
            )
        except Exception:
            url = ""
        if self._is_stale_cb is not None and self._is_stale_cb():
            return
        if url:
            try:
                from extractors.mood_engine import MOOD_ENGINE

                if MOOD_ENGINE.enabled:
                    MOOD_ENGINE.resolve_for_url(url, background=False)
            except Exception:
                pass
        if self._is_stale_cb is not None and self._is_stale_cb():
            return
        try:
            self.signals.finished.emit(
                self._request_id,
                self._track_hash,
                self._dispatched_at,
                self._cache_key,
                self._info,
                url,
            )
        except RuntimeError:
            pass


class MediaScrobbler(QThread):
    """Runs an asyncio loop that watches SMTC sessions and dispatches QThreadPool tasks for album art."""

    MEDIA_STOP_GRACE_PERIOD = MEDIA_STOP_GRACE_PERIOD

    media_changed = Signal(object)
    message = Signal(str, str)
    _start_stop_timer_sig = Signal(int)
    _cancel_stop_timer_sig = Signal()

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
        self._last_info: MediaInfo | None = None
        self._request_seq: int = 0
        self._active_request_id: int = 0
        self._active_track_hash: str = ""
        self._active_track_ts: float = 0.0
        self._stop_grace_until: float | None = None
        self._pending_art_keys: dict[str, int] = {}
        self._active_runnables: set[_ArtworkRunnable] = set()

        self._stop_timer = QTimer(self)
        self._stop_timer.setSingleShot(True)
        self._stop_timer.setInterval(int(self.MEDIA_STOP_GRACE_PERIOD * 1000))
        self._stop_timer.timeout.connect(self._on_stop_grace_expired)
        self._start_stop_timer_sig.connect(self._on_start_stop_timer)
        self._cancel_stop_timer_sig.connect(self._on_cancel_stop_timer)

    @Slot(int)
    def _on_start_stop_timer(self, interval_ms: int) -> None:
        self._stop_timer.start(max(100, int(interval_ms)))

    @Slot()
    def _on_cancel_stop_timer(self) -> None:
        if self._stop_timer.isActive():
            self._stop_timer.stop()

    @Slot()
    def _on_stop_grace_expired(self) -> None:
        with self._state_lock:
            if self._stop_grace_until is None:
                return
            self._stop_grace_until = 0.0
        self._commit_media_stop()
        self._poke()

    def _commit_media_stop(self) -> None:
        """Emit media stop and clear state only after MEDIA_STOP_GRACE_PERIOD has elapsed without resumption."""
        should_emit = False
        with self._state_lock:
            if self._stop_grace_until is None and self._last_identity is None:
                return
            if self._stop_grace_until is not None and time.monotonic() < self._stop_grace_until:
                return
            self._stop_grace_until = None
            self._active_track_hash = ""
            self._last_info = None
            if self._last_identity is not None:
                self._last_identity, self._last_start = None, None
                should_emit = True
        self._cancel_stop_timer_sig.emit()
        if should_emit:
            self.media_changed.emit(None)

    def stop(self) -> None:
        self._halt.set()
        self._cancel_stop_timer_sig.emit()
        self._poke()
        self.wait(3000)

    def _poke(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None and not loop.is_closed():
            loop.call_soon_threadsafe(wake.set)

    def _is_request_stale(self, request_id: int, track_hash: str) -> bool:
        """Thread-safe verification whether a worker request no longer matches the active SMTC track."""
        if self._halt.is_set():
            return True
        with self._state_lock:
            if not self._active_track_hash or not track_hash:
                return True
            if track_hash != self._active_track_hash:
                return True
            return request_id < self._active_request_id

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
            with self._state_lock:
                in_grace = self._stop_grace_until is not None
            poll_wait = 0.25 if in_grace else POLL_FALLBACK_SECONDS
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=poll_wait)
            except asyncio.TimeoutError:
                pass
        self._cancel_stop_timer_sig.emit()
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
        if properties is None:
            return None
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

        track_hash = make_track_hash(artist, title)
        cancel_grace = False
        with self._state_lock:
            if self._stop_grace_until is not None:
                self._stop_grace_until = None
                cancel_grace = True
            if track_hash != self._active_track_hash:
                self._request_seq += 1
                self._active_request_id = self._request_seq
                self._active_track_hash = track_hash
                self._active_track_ts = time.monotonic()
            request_id = self._active_request_id

        if cancel_grace:
            self._cancel_stop_timer_sig.emit()

        cached, artwork_url = self._art.get_cached(artist, title, album)
        info = MediaInfo(
            app_id=session.source_app_user_model_id,
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            start_at=start_at,
            end_at=end_at,
            artwork_url=sanitize_artwork_url(artwork_url),
            request_id=request_id,
            track_hash=track_hash,
        )
        return info

    def _dispatch_artwork_task(self, info: MediaInfo, request_id: int, track_hash: str) -> None:
        if self._halt.is_set():
            return
        cache_key = self._art.make_key(info.artist, info.title, info.album)
        dispatched_at = time.monotonic()
        with self._state_lock:
            if self._pending_art_keys.get(track_hash) == request_id:
                return
            self._pending_art_keys[track_hash] = request_id
            self._pending_art_keys[cache_key] = request_id

        runnable = _ArtworkRunnable(
            self._art,
            cache_key,
            info,
            request_id=request_id,
            track_hash=track_hash,
            dispatched_at=dispatched_at,
            is_stale_cb=lambda rid=request_id, th=track_hash: self._is_request_stale(rid, th),
        )
        with self._state_lock:
            self._active_runnables.add(runnable)

        def _on_done(
            req_id: int,
            req_hash: str,
            _req_ts: float,
            key: str,
            orig_info: MediaInfo,
            url: str,
            r: _ArtworkRunnable = runnable,
        ) -> None:
            with self._state_lock:
                self._active_runnables.discard(r)
                if self._pending_art_keys.get(key) == req_id:
                    self._pending_art_keys.pop(key, None)
                if self._pending_art_keys.get(req_hash) == req_id:
                    self._pending_art_keys.pop(req_hash, None)
                current_start = self._last_start

            # Discard stale worker payload immediately if user already skipped to another track
            if self._is_request_stale(req_id, req_hash):
                return

            clean_url = sanitize_artwork_url(url)
            if clean_url:
                clean_label = f"{sanitize_artist(orig_info.artist)} - {sanitize_title(orig_info.title)}".strip(" -")
                self.message.emit("info", f"album cover matched ({TARGET_ARTWORK_SIZE}): {clean_label}")
                enriched = replace(
                    orig_info,
                    artwork_url=clean_url,
                    start_at=current_start if current_start is not None else orig_info.start_at,
                    request_id=req_id,
                    track_hash=req_hash,
                )
                self._publish_artwork_update(enriched, req_id, req_hash)
                self._poke()
            else:
                fallback_icon = fallback_artwork_for_app(orig_info.app_id)
                self.message.emit(
                    "info",
                    f"cover score below threshold or unavailable for '{orig_info.title}', using fallback icon ({fallback_icon})",
                )

        runnable.signals.finished.connect(_on_done, Qt.ConnectionType.DirectConnection)
        self._pool.start(runnable)

    def _publish_artwork_update(self, enriched: MediaInfo, req_id: int, req_hash: str) -> None:
        """Publish asynchronous artwork enrichment only if the track is still the active SMTC session."""
        if self._is_request_stale(req_id, req_hash):
            return
        emit_payload: MediaInfo | None = None
        with self._state_lock:
            if (
                self._halt.is_set()
                or not self._active_track_hash
                or req_hash != self._active_track_hash
                or req_id < self._active_request_id
            ):
                return
            base = (
                self._last_info
                if (self._last_info is not None and self._last_info.track_hash == req_hash)
                else enriched
            )
            updated = replace(
                base,
                artwork_url=enriched.artwork_url,
                start_at=self._last_start if self._last_start is not None else base.start_at,
                request_id=self._active_request_id,
                track_hash=req_hash,
            )
            if updated.identity != self._last_identity:
                self._last_info = updated
                self._last_identity = updated.identity
                emit_payload = updated
        if emit_payload is not None:
            self.media_changed.emit(emit_payload)

    def _publish(self, info: MediaInfo | None) -> None:
        if info is None:
            should_start_timer = False
            should_commit_stop = False
            with self._state_lock:
                if self._last_identity is None:
                    self._stop_grace_until = None
                    return
                now = time.monotonic()
                if self._stop_grace_until is None:
                    self._stop_grace_until = now + self.MEDIA_STOP_GRACE_PERIOD
                    should_start_timer = True
                elif now >= self._stop_grace_until:
                    should_commit_stop = True
            if should_start_timer:
                self._start_stop_timer_sig.emit(int(self.MEDIA_STOP_GRACE_PERIOD * 1000))
            elif should_commit_stop:
                self._commit_media_stop()
            return

        cancel_grace = False
        should_emit = False
        need_artwork_fetch = False
        with self._state_lock:
            if self._stop_grace_until is not None:
                self._stop_grace_until = None
                cancel_grace = True

            # Synchronize active sequence ID & hash if _publish was invoked with a newer track
            if info.track_hash and info.track_hash != self._active_track_hash:
                self._request_seq += 1
                self._active_request_id = self._request_seq
                self._active_track_hash = info.track_hash
                self._active_track_ts = time.monotonic()
                info = replace(info, request_id=self._active_request_id)

            # Preserve resolved artwork URL if the same track is still playing
            if not info.artwork_url:
                if (
                    self._last_info is not None
                    and self._last_info.track_hash == info.track_hash
                    and self._last_info.artwork_url
                ):
                    info = replace(info, artwork_url=self._last_info.artwork_url)
                elif (
                    self._last_identity is not None
                    and self._last_identity[:3] == (info.app_id, info.title, info.artist)
                    and self._last_identity[4]
                ):
                    info = replace(info, artwork_url=self._last_identity[4])

            changed = info.identity != self._last_identity
            sought = (
                info.start_at is not None
                and self._last_start is not None
                and abs(info.start_at - self._last_start) > SEEK_TOLERANCE_SECONDS
            )
            self._last_info = info
            if changed or sought:
                self._last_identity, self._last_start = info.identity, info.start_at
                should_emit = True

            if not info.artwork_url:
                cached, cached_url = self._art.get_cached(info.artist, info.title, info.album)
                if cached and cached_url:
                    info = replace(info, artwork_url=sanitize_artwork_url(cached_url))
                    self._last_info = info
                    self._last_identity = info.identity
                    should_emit = True
                elif not cached:
                    need_artwork_fetch = True

        if cancel_grace:
            self._cancel_stop_timer_sig.emit()
        if should_emit:
            self.media_changed.emit(info)
        if need_artwork_fetch:
            self._dispatch_artwork_task(info, info.request_id, info.track_hash)



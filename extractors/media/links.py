"""Interactive Rich Presence button builders: Spotify deep link and Genius lyrics search."""
from __future__ import annotations

import urllib.parse

DISCORD_BUTTON_LABEL_LIMIT = 32
DISCORD_BUTTON_URL_LIMIT = 512

SPOTIFY_LABEL = "Listen on Spotify"
GENIUS_LABEL = "Lyrics on Genius"
SPOTIFY_SEARCH_BASE = "https://open.spotify.com/search/"
SPOTIFY_TRACK_PREFIX = "https://open.spotify.com/track/"
GENIUS_SEARCH_BASE = "https://genius.com/search?q="


def _query(title: str, artist: str) -> str:
    return " ".join(part for part in ((title or "").strip(), (artist or "").strip()) if part)


def _fit_url(base: str, query: str) -> str:
    """Percent-encode `query` and shrink it until base + query fits Discord's URL limit."""
    candidate = query
    while candidate:
        url = base + urllib.parse.quote(candidate, safe="")
        if len(url) <= DISCORD_BUTTON_URL_LIMIT:
            return url
        candidate = candidate[: max(0, len(candidate) - 16)].rstrip()
    return ""


def is_spotify_track_url(url: str) -> bool:
    cleaned = (url or "").strip()
    return (
        cleaned.startswith(SPOTIFY_TRACK_PREFIX)
        and len(cleaned) <= DISCORD_BUTTON_URL_LIMIT
        and not any(ch.isspace() for ch in cleaned)
    )


def spotify_track_url_from_id(raw: str) -> str:
    """Accept a MPRIS trackid ('/com/spotify/track/<id>'), a spotify:track URI, or a full URL."""
    text = (raw or "").strip()
    if not text:
        return ""
    if is_spotify_track_url(text):
        return text.split("?", 1)[0]
    for marker in ("/com/spotify/track/", "spotify:track:"):
        if marker in text:
            track_id = text.split(marker, 1)[1].split("?", 1)[0].strip("/ ")
            if track_id.isalnum():
                return SPOTIFY_TRACK_PREFIX + track_id
    return ""


def build_spotify_url(title: str, artist: str, track_url: str = "") -> str:
    """Specific track URL when the Spotify ID is known, otherwise a search deep link."""
    if is_spotify_track_url(track_url):
        return track_url.strip()
    return _fit_url(SPOTIFY_SEARCH_BASE, _query(title, artist))


def build_genius_url(title: str, artist: str) -> str:
    return _fit_url(GENIUS_SEARCH_BASE, _query(title, artist))


def build_media_buttons(title: str, artist: str, track_url: str = "") -> tuple[tuple[str, str], ...]:
    """Return up to two (label, url) pairs that satisfy Discord's label/URL limits."""
    pairs: list[tuple[str, str]] = []
    spotify = build_spotify_url(title, artist, track_url)
    if spotify:
        pairs.append((SPOTIFY_LABEL[:DISCORD_BUTTON_LABEL_LIMIT], spotify))
    genius = build_genius_url(title, artist)
    if genius:
        pairs.append((GENIUS_LABEL[:DISCORD_BUTTON_LABEL_LIMIT], genius))
    return tuple(pairs)

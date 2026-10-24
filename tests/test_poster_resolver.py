"""Unit tests for VLC Dynamic Online Poster/Thumbnail Resolver and Paused character asset display."""
from __future__ import annotations

import io
import json
import socket
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from core.priority_engine import EngineInput, PriorityEngine, RuleBook
from extractors.media.common import MediaInfo
from extractors.media.poster_resolver import (
    ANILIST_GRAPHQL_URL,
    DEFAULT_FALLBACK_ASSET,
    PAUSE_ASSET,
    POSTER_RESOLVER,
    REQUEST_TIMEOUT,
    PosterResolver,
    normalize_poster_title,
)

ROOT = Path(__file__).resolve().parents[1]


def setUpModule() -> None:
    QApplication.instance() or QApplication([])


class _MockHTTPResponse:
    def __init__(self, payload: dict | bytes, status: int = 200) -> None:
        if isinstance(payload, dict):
            self._bytes = json.dumps(payload).encode("utf-8")
        else:
            self._bytes = payload
        self.status = status

    def read(self, amt: int = -1) -> bytes:
        return self._bytes if amt < 0 else self._bytes[:amt]

    def __enter__(self) -> "_MockHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _RecordingOpener:
    """Deterministic opener for testing AniList GraphQL and TVMaze HTTP calls."""

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: urllib.request.Request, timeout: float = 1.5):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if not self._responses:
            raise urllib.error.URLError("no more mock responses configured")
        nxt = self._responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        if isinstance(nxt, type) and issubclass(nxt, BaseException):
            raise nxt()
        return _MockHTTPResponse(nxt)


def _make_engine() -> PriorityEngine:
    book = RuleBook.load(ROOT / "config" / "rules.json").with_enabled("vlc-video", True).with_enabled("vlc-audio", True)
    return PriorityEngine(
        book,
        idle_threshold=10_000,
        show_standby=True,
        show_buttons=False,
        mood_engine_enabled=False,
        celestial_sync_enabled=False,
        privacy_show_city=False,
        idle_custom_image="",
    )


def _resolve_engine(engine: PriorityEngine, media: MediaInfo):
    return engine.resolve(
        EngineInput(
            now=time.time(),
            activities=[],
            media=media,
            manual=None,
            paused=False,
            idle_seconds=0.0,
            shared_variables={},
        )
    )


class TestPosterTitleNormalization(unittest.TestCase):
    """Verify title normalization removes episode numbers, BD/release tags, and resolutions."""

    def test_clean_anime_and_series_titles(self) -> None:
        cases = {
            "Clannad BD - 01": "Clannad",
            "Clannad BD - 02 [1080p].mkv": "Clannad",
            "One Piece - 1089 [1080p]": "One Piece",
            "[SubsPlease] Sousou no Frieren - 05 (1080p) [ABCD1234].mkv": "Sousou no Frieren",
            "Attack on Titan S04E12 [1080p] [x265].mkv": "Attack on Titan",
            "Breaking.Bad.S01E01.1080p.BluRay.x265.mp4": "Breaking Bad",
            "Steins;Gate - Episode 24 [BD 1080p FLAC]": "Steins;Gate",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_poster_title(raw), expected)


class TestPosterResolverOnlineFetching(unittest.TestCase):
    """Verify AniList primary fetch, TVMaze fallback, TTL cache reuse across episodes, and error fallback."""

    def setUp(self) -> None:
        POSTER_RESOLVER.clear_cache()

    def tearDown(self) -> None:
        POSTER_RESOLVER.clear_cache()

    def test_anilist_success_returns_https_url_and_caches_across_episodes(self) -> None:
        clannad_poster = "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/bx2167-clannad.jpg"
        anilist_payload = {
            "data": {
                "Media": {
                    "id": 2167,
                    "title": {"romaji": "Clannad", "english": "Clannad", "native": "CLANNAD"},
                    "coverImage": {
                        "extraLarge": clannad_poster,
                        "large": clannad_poster,
                        "medium": "https://s4.anilist.co/file/anilistcdn/media/anime/cover/medium/bx2167-clannad.jpg",
                    },
                }
            }
        }
        opener = _RecordingOpener([anilist_payload])
        resolver = PosterResolver(opener=opener)

        # Episode 1 triggers network lookup for normalized title "Clannad"
        url_ep1 = resolver.fetch_sync("Clannad BD - 01")
        self.assertEqual(url_ep1, clannad_poster)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(opener.requests[0].full_url, ANILIST_GRAPHQL_URL)
        self.assertLessEqual(opener.timeouts[0], REQUEST_TIMEOUT)
        self.assertIn("clannad", resolver.cache)
        self.assertEqual(resolver.cache["clannad"], clannad_poster)

        # Episode 2 of the same series hits the in-memory cache with 0 additional HTTP requests
        url_ep2 = resolver.fetch_sync("Clannad BD - 02 [1080p].mkv")
        self.assertEqual(url_ep2, clannad_poster)
        self.assertEqual(len(opener.requests), 1)

    def test_tvmaze_fallback_when_anilist_misses(self) -> None:
        anilist_empty = {"data": {"Media": None}}
        tvmaze_poster = "https://static.tvmaze.com/uploads/images/medium_portrait/0/2400.jpg"
        tvmaze_payload = {
            "id": 169,
            "name": "Breaking Bad",
            "image": {
                "medium": tvmaze_poster,
                "original": "https://static.tvmaze.com/uploads/images/original_untouched/0/2400.jpg",
            },
        }
        opener = _RecordingOpener([anilist_empty, tvmaze_payload])
        resolver = PosterResolver(opener=opener)

        url = resolver.fetch_sync("Breaking Bad S01E01 1080p")
        self.assertEqual(url, tvmaze_poster)
        self.assertEqual(len(opener.requests), 2)
        self.assertIn("api.tvmaze.com/singlesearch/shows?q=Breaking%20Bad", opener.requests[1].full_url)

    def test_timeout_and_network_errors_fallback_smoothly_to_vlc(self) -> None:
        errors = [
            socket.timeout("timed out"),
            TimeoutError("request timed out"),
            urllib.error.URLError(ConnectionRefusedError(10061, "Connection refused")),
            urllib.error.HTTPError(ANILIST_GRAPHQL_URL, 503, "Service Unavailable", {}, None),
        ]
        for err in errors:
            with self.subTest(error=type(err).__name__):
                opener = _RecordingOpener([err, err])
                resolver = PosterResolver(opener=opener)
                result = resolver.fetch_sync("One Piece - 1089 [1080p]")
                self.assertEqual(result, DEFAULT_FALLBACK_ASSET)

    def test_home_video_skips_network_and_falls_back_to_vlc(self) -> None:
        opener = _RecordingOpener([])
        resolver = PosterResolver(opener=opener)
        self.assertEqual(resolver.fetch_sync("VID_20261024_193012.mp4"), "vlc")
        self.assertEqual(resolver.fetch_sync("My_Home_Video.avi"), "vlc")
        self.assertEqual(len(opener.requests), 0)


class TestVlcPlayingAndPausedPresenceDisplay(unittest.TestCase):
    """Verify VLC Rich Presence payload during Playing -> Paused -> Playing transitions."""

    def setUp(self) -> None:
        POSTER_RESOLVER.clear_cache()
        self.engine = _make_engine()

    def tearDown(self) -> None:
        POSTER_RESOLVER.clear_cache()

    def test_playing_paused_resume_transitions_with_online_poster(self) -> None:
        poster_url = "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/bx21-onepiece.jpg"
        anilist_response = {
            "data": {
                "Media": {
                    "id": 21,
                    "title": {"romaji": "ONE PIECE"},
                    "coverImage": {"large": poster_url},
                }
            }
        }
        opener = _RecordingOpener([anilist_response])
        now = time.time()

        with patch.object(POSTER_RESOLVER, "_opener", opener):
            # 1. State PLAYING: large_image = poster_url, large_text = video title, small_image = "vlc", active timeline
            playing_media = MediaInfo(
                app_id="vlc",
                title="One Piece - 1089",
                artist="",
                album="",
                duration=1440.0,
                start_at=now - 120.0,
                end_at=now - 120.0 + 1440.0,
                status="Playing",
                kind="video",
            )
            res_playing = _resolve_engine(self.engine, playing_media)
            self.assertEqual(res_playing.rule.id, "vlc-video")
            self.assertEqual(res_playing.presence.large_image, poster_url)
            self.assertEqual(res_playing.presence.large_text, "One Piece - 1089")
            self.assertEqual(res_playing.presence.small_image, "vlc")
            self.assertIsNotNone(res_playing.presence.start)
            self.assertIsNotNone(res_playing.presence.end)

            # 2. State PAUSED: large_image = "pause", large_text = "Dijeda - {judul}", timeline frozen (None)
            paused_media = MediaInfo(
                app_id="vlc",
                title="One Piece - 1089",
                artist="",
                album="",
                duration=1440.0,
                start_at=None,
                end_at=None,
                status="Paused",
                kind="video",
            )
            res_paused = _resolve_engine(self.engine, paused_media)
            self.assertEqual(res_paused.rule.id, "vlc-video")
            self.assertEqual(res_paused.presence.large_image, PAUSE_ASSET)
            self.assertEqual(res_paused.presence.large_text, "Dijeda - One Piece - 1089")
            self.assertIsNone(res_paused.presence.start)
            self.assertIsNone(res_paused.presence.end)

            # 3. RESUME to PLAYING: large_image immediately returns to cached poster_url without extra HTTP calls
            resumed_media = MediaInfo(
                app_id="vlc",
                title="One Piece - 1089",
                artist="",
                album="",
                duration=1440.0,
                start_at=now - 300.0,
                end_at=now - 300.0 + 1440.0,
                status="Playing",
                kind="video",
            )
            res_resumed = _resolve_engine(self.engine, resumed_media)
            self.assertEqual(res_resumed.presence.large_image, poster_url)
            self.assertEqual(res_resumed.presence.large_text, "One Piece - 1089")
            self.assertEqual(res_resumed.presence.small_image, "vlc")
            self.assertIsNotNone(res_resumed.presence.start)
            self.assertIsNotNone(res_resumed.presence.end)
            self.assertEqual(len(opener.requests), 1)

    def test_playing_fallback_to_vlc_asset_when_offline(self) -> None:
        opener = _RecordingOpener([socket.timeout("offline"), socket.timeout("offline")])
        now = time.time()
        with patch.object(POSTER_RESOLVER, "_opener", opener):
            media = MediaInfo(
                app_id="vlc",
                title="Clannad BD - 01",
                artist="",
                album="",
                duration=1440.0,
                start_at=now - 60.0,
                end_at=now - 60.0 + 1440.0,
                status="Playing",
                kind="video",
            )
            res = _resolve_engine(self.engine, media)
            self.assertEqual(res.presence.large_image, "vlc")
            self.assertEqual(res.presence.large_text, "Clannad BD - 01")
            self.assertIsNone(res.presence.small_image)


if __name__ == "__main__":
    unittest.main()

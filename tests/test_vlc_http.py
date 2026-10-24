"""Hermetic tests for VLC detection through the HTTP web interface (`/requests/status.json`)."""
from __future__ import annotations

import base64
import json
import socket
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QCoreApplication

from core.priority_engine import Activity, EngineInput, PriorityEngine, RuleBook
from core.settings_manager import Settings
from extractors.media.base import BaseMediaExtractor, RawTrack
from extractors.media.common import MediaInfo
from extractors.media.vlc_http import VlcHttpClient, VlcHttpConfig
from extractors.media.windows_smtc import WindowsSmtcExtractor

ROOT = Path(__file__).resolve().parents[1]
CONFIG = VlcHttpConfig(enabled=True, host="127.0.0.1", port=8080, password="omnipresence")


def setUpModule() -> None:
    QCoreApplication.instance() or QCoreApplication([])


def status_json(state="playing", time_s=50, length=200, meta=None, extra_category=None) -> dict:
    category = {"meta": meta if meta is not None else {}}
    category.update(extra_category or {})
    return {"state": state, "time": time_s, "length": length, "information": {"category": category}}


AUDIO = {"filename": "Sialan.flac", "title": "Sialan", "artist": "Juicy Luicy", "album": "Nonfiksi"}
VIDEO = {"filename": "[Kuso] Spider Verse (2023) [1080p] [x265] [Subs].mkv"}


class FakeResponse:
    def __init__(self, payload) -> None:
        self._body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class FakeOpener:
    """Stands in for urllib's opener. `script` items are payloads or exceptions, consumed in order."""

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.requests: list = []
        self.timeouts: list = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return FakeResponse(item)


def make_client(*script, config: VlcHttpConfig = CONFIG, clock=time.time) -> tuple[VlcHttpClient, FakeOpener]:
    opener = FakeOpener(*script)
    return VlcHttpClient(lambda: config, opener=opener, clock=clock), opener


def make_engine() -> PriorityEngine:
    book = RuleBook.load(ROOT / "config" / "rules.json").with_enabled("browser-youtube", True).with_enabled("spotify", True)
    return PriorityEngine(
        book, 10_000, True, False, mood_engine_enabled=False, celestial_sync_enabled=False,
        privacy_show_city=False, idle_custom_image="",
    )


def to_media_info(raw: RawTrack) -> MediaInfo:
    return MediaInfo(
        app_id=raw.app_id, title=raw.title, artist=raw.artist, album=raw.album, duration=raw.duration,
        start_at=raw.start_at, end_at=raw.end_at, track_hash=raw.state_hash, status=raw.status, kind=raw.kind,
    )


def resolve(engine, media=None, activities=()):
    return engine.resolve(
        EngineInput(
            now=time.time(), activities=list(activities), media=media, manual=None, paused=False,
            idle_seconds=0.0, shared_variables={},
        )
    )


class VlcHttpParsingTests(unittest.TestCase):
    def test_request_uses_basic_auth_with_empty_username(self) -> None:
        client, opener = make_client(status_json(meta=AUDIO))
        client.fetch()
        request = opener.requests[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8080/requests/status.json")
        expected = "Basic " + base64.b64encode(b":omnipresence").decode()
        self.assertEqual(request.get_header("Authorization"), expected)
        self.assertLessEqual(opener.timeouts[0], 2.0)

    def test_playing_audio_has_spotify_style_timeline(self) -> None:
        now = 1_800_000_000.0
        client, _ = make_client(status_json("playing", 50, 200, AUDIO), clock=lambda: now)
        raw = client.fetch()
        self.assertEqual((raw.app_id, raw.kind, raw.status), ("vlc", "audio", "Playing"))
        self.assertEqual((raw.title, raw.artist, raw.album), ("Sialan", "Juicy Luicy", "Nonfiksi"))
        self.assertEqual(raw.start_at, now - 50)  # start = now - time
        self.assertEqual(raw.end_at, now - 50 + 200)  # end = start + length
        presence = resolve(make_engine(), to_media_info(raw)).presence
        self.assertEqual(presence.activity_type, 2)  # Listening
        self.assertEqual(presence.end - presence.start, 200)

    def test_video_filename_is_sanitized_and_watching(self) -> None:
        client, _ = make_client(status_json("playing", 600, 7200, VIDEO))
        raw = client.fetch()
        self.assertEqual(raw.kind, "video")
        self.assertEqual(raw.title, "Spider Verse (2023)")
        result = resolve(make_engine(), to_media_info(raw))
        self.assertEqual(result.rule.id, "vlc-video")
        self.assertEqual(result.presence.activity_type, 3)  # Watching

    def test_paused_clears_end_and_shows_pause_indicator(self) -> None:
        client, _ = make_client(status_json("paused", 50, 200, AUDIO))
        raw = client.fetch()
        self.assertEqual(raw.status, "Paused")
        self.assertIsNone(raw.end_at)
        self.assertIsNone(raw.start_at)
        presence = resolve(make_engine(), to_media_info(raw)).presence
        self.assertIsNone(presence.end)
        self.assertIsNone(presence.start)
        self.assertEqual(presence.small_image, "pause")
        self.assertEqual(presence.small_text, "Dijeda")
        self.assertTrue(presence.details.startswith("[Paused]"))

    def test_stopped_returns_none(self) -> None:
        client, _ = make_client(status_json("stopped", 0, 0, {}))
        self.assertIsNone(client.fetch())
        self.assertTrue(client.reachable)  # VLC answered, it is simply stopped

    def test_live_stream_has_running_time_only(self) -> None:
        now = 1_800_000_000.0
        meta = {"title": "Radio Stream", "filename": "live"}
        client, _ = make_client(status_json("playing", 30, 0, meta), clock=lambda: now)
        raw = client.fetch()
        self.assertEqual(raw.duration, 0.0)
        self.assertIsNone(raw.end_at)
        self.assertEqual(raw.start_at, now - 30)

    def test_kind_falls_back_to_video_stream_type(self) -> None:
        streams = {"Stream 0": {"Type": "Video", "Codec": "H264"}}
        client, _ = make_client(status_json("playing", 1, 100, {"title": "Untitled clip"}, streams))
        self.assertEqual(client.fetch().kind, "video")

    def test_missing_title_and_filename_is_ignored(self) -> None:
        client, _ = make_client(status_json("playing", 1, 100, {}))
        self.assertIsNone(client.fetch())


class VlcHttpFailureTests(unittest.TestCase):
    def test_connection_refused_is_silent(self) -> None:
        for error in (
            ConnectionRefusedError(10061, "refused"),
            urllib.error.URLError(ConnectionRefusedError(10061, "refused")),
        ):
            with self.subTest(error=type(error).__name__):
                client, _ = make_client(error)
                self.assertIsNone(client.fetch())
                self.assertFalse(client.reachable)
                self.assertEqual(client.last_error, "unreachable")
                self.assertIsNone(client.pop_notice())

    def test_timeouts_are_handled(self) -> None:
        for error in (socket.timeout("timed out"), TimeoutError(), urllib.error.URLError(socket.timeout("t"))):
            with self.subTest(error=type(error).__name__):
                client, _ = make_client(error)
                self.assertIsNone(client.fetch())
                self.assertEqual(client.last_error, "timeout")

    def test_wrong_password_warns_once(self) -> None:
        err = urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)
        client, _ = make_client(err)
        self.assertIsNone(client.fetch())
        self.assertIn("password", client.pop_notice())
        client.fetch()
        self.assertIsNone(client.pop_notice())  # not repeated every poll

    def test_invalid_json_and_non_object_payloads(self) -> None:
        for body in (b"<html>not json</html>", b"[1, 2, 3]", b""):
            with self.subTest(body=body):
                client, _ = make_client(body)
                self.assertIsNone(client.fetch())

    def test_disabled_never_touches_the_network(self) -> None:
        client, opener = make_client(status_json(meta=AUDIO), config=VlcHttpConfig(enabled=False))
        self.assertIsNone(client.fetch())
        self.assertEqual(opener.requests, [])

    def test_recovers_after_vlc_starts(self) -> None:
        client, _ = make_client(ConnectionRefusedError(), status_json("playing", 5, 100, AUDIO))
        self.assertIsNone(client.fetch())
        self.assertIsNotNone(client.fetch())
        self.assertTrue(client.reachable)

    def test_vlc_closed_falls_back_to_youtube_then_spotify(self) -> None:
        engine = make_engine()
        youtube = Activity("browser-youtube", "yt", {"video": "Some Video", "app_label": "Chrome"})
        spotify = MediaInfo("spotify", "Track", "Artist", "", 100.0, None, None)
        client, _ = make_client(status_json("playing", 5, 100, AUDIO), ConnectionRefusedError())
        vlc = to_media_info(client.fetch())
        with mock.patch("extractors.youtube_resolver.YOUTUBE_THUMBNAIL_RESOLVER.resolve", return_value=""):
            self.assertEqual(resolve(engine, vlc, [youtube]).rule.id, "vlc-audio")
            self.assertIsNone(client.fetch())  # VLC quit
            engine.clear_media_grace()
            self.assertEqual(resolve(engine, None, [youtube]).rule.id, "browser-youtube")
            self.assertEqual(resolve(engine, spotify, []).rule.id, "spotify")


class _HttpOnlyExtractor(BaseMediaExtractor):
    """Minimal backend that exposes only the VLC HTTP client, to exercise the shared polling logic."""

    def __init__(self, client: VlcHttpClient, art_resolver) -> None:
        super().__init__(art_resolver=art_resolver)
        self._client = client

    def _read_session(self):
        return self._client.fetch()


class VlcHttpPollingTests(unittest.TestCase):
    def _run(self, script, clock):
        from extractors.mood_engine import MOOD_ENGINE

        client, _ = make_client(*script, clock=clock)
        resolver = mock.MagicMock()
        resolver.get_cached.return_value = (True, "")
        extractor = _HttpOnlyExtractor(client, resolver)
        emitted: list = []
        extractor.media_changed.connect(emitted.append)
        patcher = mock.patch.object(MOOD_ENGINE, "enabled", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        return extractor, emitted

    def test_playing_paused_playing_and_seek_each_publish(self) -> None:
        clock = {"now": 1_800_000_000.0}
        script = [
            status_json("playing", 10, 200, AUDIO),
            status_json("playing", 12, 200, AUDIO),  # steady playback: silent
            status_json("paused", 12, 200, AUDIO),
            status_json("playing", 12, 200, AUDIO),
            status_json("playing", 120, 200, AUDIO),  # user seeks forward
            status_json("stopped", 0, 0, {}),
        ]
        extractor, emitted = self._run(script, lambda: clock["now"])
        for _ in range(5):
            extractor._poll_once()
            clock["now"] += 2.0
        statuses = [(m.status, m.start_at is not None) for m in emitted]
        self.assertEqual(statuses, [("Playing", True), ("Paused", False), ("Playing", True), ("Playing", True)])
        self.assertGreater(abs(emitted[3].start_at - emitted[2].start_at), 90)  # seek moved the bar

        extractor._poll_once()  # stopped: needs two empty ticks before releasing
        self.assertEqual(len(emitted), 4)
        extractor._poll_once()
        self.assertIsNone(emitted[-1])


class WindowsIntegrationTests(unittest.TestCase):
    @staticmethod
    def _extractor(client: VlcHttpClient) -> tuple[WindowsSmtcExtractor, list]:
        extractor = WindowsSmtcExtractor(vlc_client=client)
        calls: list = []

        async def fake_read_async(skip_vlc: bool = False):
            calls.append(skip_vlc)
            return None

        extractor._read_async = fake_read_async  # type: ignore[method-assign]
        return extractor, calls

    def test_poll_interval_is_fast_for_vlc(self) -> None:
        self.assertEqual(WindowsSmtcExtractor.POLL_INTERVAL_MS, 250)

    def test_vlc_http_wins_and_skips_smtc(self) -> None:
        client, _ = make_client(status_json("playing", 5, 100, AUDIO))
        extractor, calls = self._extractor(client)
        raw = extractor._read_session()
        self.assertEqual(raw.app_id, "vlc")
        self.assertEqual(calls, [])

    def test_unreachable_vlc_falls_back_to_smtc_including_vlc_sessions(self) -> None:
        client, _ = make_client(ConnectionRefusedError())
        extractor, calls = self._extractor(client)
        self.assertIsNone(extractor._read_session())
        self.assertEqual(calls, [False])

    def test_stopped_vlc_hides_stale_vlc_smtc_session(self) -> None:
        client, _ = make_client(status_json("stopped", 0, 0, {}))
        extractor, calls = self._extractor(client)
        self.assertIsNone(extractor._read_session())
        self.assertEqual(calls, [True])

    def test_wrong_password_emits_a_warning_message(self) -> None:
        err = urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)
        client, _ = make_client(err)
        extractor, _ = self._extractor(client)
        messages: list = []
        extractor.message.connect(lambda level, text: messages.append((level, text)))
        extractor._read_session()
        self.assertEqual(messages[0][0], "warn")
        self.assertIn("password", messages[0][1])


class LinuxFallbackTests(unittest.TestCase):
    @staticmethod
    def _bus(players: dict):
        from tests.test_vlc_mpris import make_fake_bus

        return make_fake_bus(players)

    def test_http_is_used_when_vlc_has_no_mpris_player(self) -> None:
        from extractors.media.linux_mpris import LinuxMprisExtractor

        spotify = {"PlaybackStatus": "Playing", "Metadata": {"xesam:title": "Other"}, "Position": 0}
        bus = self._bus({"org.mpris.MediaPlayer2.spotify": spotify})
        client, opener = make_client(status_json("playing", 5, 100, AUDIO))
        raw = LinuxMprisExtractor(bus_factory=lambda: bus, vlc_client=client)._read_session()
        self.assertEqual(raw.app_id, "vlc")  # VLC (HTTP) still outranks Spotify (MPRIS)
        self.assertEqual(len(opener.requests), 1)

    def test_mpris_vlc_is_primary_and_http_is_not_polled(self) -> None:
        from extractors.media.linux_mpris import LinuxMprisExtractor
        from tests.test_vlc_mpris import AUDIO_META, vlc_props

        bus = self._bus({"org.mpris.MediaPlayer2.vlc": vlc_props("Playing", AUDIO_META, 5)})
        client, opener = make_client(status_json("playing", 99, 100, AUDIO))
        raw = LinuxMprisExtractor(bus_factory=lambda: bus, vlc_client=client)._read_session()
        self.assertEqual(raw.title, "Sialan")
        self.assertEqual(opener.requests, [])

    def test_http_refused_and_no_players_yields_none(self) -> None:
        from extractors.media.linux_mpris import LinuxMprisExtractor

        client, _ = make_client(ConnectionRefusedError())
        self.assertIsNone(LinuxMprisExtractor(bus_factory=lambda: self._bus({}), vlc_client=client)._read_session())


class VlcSettingsTests(unittest.TestCase):
    def test_defaults_and_overrides(self) -> None:
        defaults = Settings()
        self.assertTrue(defaults.vlc_http_enabled)
        self.assertEqual((defaults.vlc_http_host, defaults.vlc_http_port), ("127.0.0.1", 8080))
        self.assertEqual(defaults.vlc_http_password, "omnipresence")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"vlc_http_password": "", "vlc_http_port": "9090", "vlc_http_enabled": False}))
            loaded = Settings.load(path)
        self.assertEqual((loaded.vlc_http_password, loaded.vlc_http_port, loaded.vlc_http_enabled), ("", 9090, False))


if __name__ == "__main__":
    unittest.main()

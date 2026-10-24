"""Hermetic tests for VLC Media Player detection (MPRIS metadata, sanitizer, P5-boosted priority rules)."""
from __future__ import annotations

import time
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QCoreApplication

from core.priority_engine import Activity, EngineInput, PriorityEngine, RuleBook, Tier
from extractors.media.base import RawTrack
from extractors.media.common import (
    MediaInfo,
    classify_media_kind,
    sanitize_media_title,
)
from extractors.media.linux_mpris import MPRIS_PATH, LinuxMprisExtractor
from extractors.media.vlc_http import VlcHttpClient, VlcHttpConfig
from extractors.media.windows_smtc import anchored_position

ROOT = Path(__file__).resolve().parents[1]
VLC = "org.mpris.MediaPlayer2.vlc"
SPOTIFY = "org.mpris.MediaPlayer2.spotify"
NO_VLC_HTTP = VlcHttpClient(lambda: VlcHttpConfig(enabled=False))  # keeps MPRIS tests off the network

AUDIO_META = {
    "mpris:length": 200_000_000,
    "xesam:title": "Sialan",
    "xesam:artist": ["Juicy Luicy"],
    "xesam:album": "Nonfiksi",
    "xesam:url": "file:///home/raf/Music/Sialan.flac",
}
VIDEO_META = {
    "mpris:length": 7_200_000_000,
    "xesam:title": "Spider.Verse.2023.1080p.BluRay.x265-GRP.mkv",
    "xesam:url": "file:///home/raf/Videos/Spider.Verse.2023.1080p.BluRay.x265-GRP.mkv",
}


def setUpModule() -> None:
    QCoreApplication.instance() or QCoreApplication([])


def make_fake_bus(players: dict[str, dict]) -> mock.MagicMock:
    proxies = {}
    for name, props in players.items():
        proxy = mock.MagicMock(name=name)
        proxy.Get.side_effect = lambda iface, prop, dbus_interface=None, _p=props: _p[prop]
        proxies[name] = proxy
    bus = mock.MagicMock(name="SessionBus")
    bus.list_names.return_value = ["org.freedesktop.DBus", *players.keys(), ":1.77"]

    def get_object(name: str, path: str):
        assert path == MPRIS_PATH
        return proxies[name]

    bus.get_object.side_effect = get_object
    return bus


def vlc_props(status: str, metadata: dict, position_s: float = 0.0) -> dict:
    return {"PlaybackStatus": status, "Metadata": metadata, "Position": int(position_s * 1_000_000)}


def read_vlc(status: str, metadata: dict, position_s: float = 0.0) -> RawTrack:
    bus = make_fake_bus({VLC: vlc_props(status, metadata, position_s)})
    raw = LinuxMprisExtractor(bus_factory=lambda: bus, vlc_client=NO_VLC_HTTP)._read_session()
    assert raw is not None
    return raw


def to_media_info(raw: RawTrack) -> MediaInfo:
    return MediaInfo(
        app_id=raw.app_id,
        title=raw.title,
        artist=raw.artist,
        album=raw.album,
        duration=raw.duration,
        start_at=raw.start_at,
        end_at=raw.end_at,
        track_hash=raw.state_hash,
        status=raw.status,
        kind=raw.kind,
    )


def make_engine() -> PriorityEngine:
    return PriorityEngine(
        RuleBook.load(ROOT / "config" / "rules.json").with_enabled("vscode", True).with_enabled("game-known", True),
        idle_threshold=10_000,
        show_standby=True,
        show_buttons=False,
        mood_engine_enabled=False,
        celestial_sync_enabled=False,
        privacy_show_city=False,
        idle_custom_image="",
    )


def resolve(engine: PriorityEngine, media=None, activities=(), manual=None):
    return engine.resolve(
        EngineInput(
            now=time.time(),
            activities=list(activities),
            media=media,
            manual=manual,
            paused=False,
            idle_seconds=0.0,
            shared_variables={},
        )
    )


EDITOR = Activity("vscode", "proj", {"file": "main.py", "workspace": "OmniPresence"})
GAME = Activity("game-known", "game", {"title": "Minecraft", "app_label": "Minecraft"})


class VlcExtractionTests(unittest.TestCase):
    def test_audio_playing_has_spotify_style_timestamps_and_listening(self) -> None:
        before = time.time()
        raw = read_vlc("Playing", AUDIO_META, position_s=50)
        self.assertEqual((raw.app_id, raw.kind, raw.status), ("vlc", "audio", "Playing"))
        self.assertEqual((raw.title, raw.artist, raw.album), ("Sialan", "Juicy Luicy", "Nonfiksi"))
        self.assertAlmostEqual(raw.duration, 200.0)
        self.assertAlmostEqual(raw.start_at, before - 50.0, delta=2.0)
        self.assertAlmostEqual(raw.end_at - raw.start_at, 200.0)  # end = start + total duration

        result = resolve(make_engine(), to_media_info(raw))
        presence = result.presence
        self.assertEqual(result.rule.id, "vlc-audio")
        self.assertEqual(presence.activity_type, 2)  # Listening
        self.assertEqual(presence.details, "Sialan")
        self.assertEqual(presence.state, "by Juicy Luicy")
        self.assertEqual(presence.end - presence.start, 200)
        self.assertEqual(presence.small_image, "playing")

    def test_video_filename_is_sanitized_and_watching(self) -> None:
        raw = read_vlc("Playing", VIDEO_META, position_s=600)
        self.assertEqual(raw.kind, "video")
        self.assertEqual(raw.title, "Spider Verse 2023")
        self.assertEqual(raw.artist, "")

        result = resolve(make_engine(), to_media_info(raw))
        self.assertEqual(result.rule.id, "vlc-video")
        self.assertEqual(result.presence.activity_type, 3)  # Watching
        self.assertEqual(result.presence.details, "Spider Verse 2023")
        self.assertEqual(result.presence.end - result.presence.start, 7200)

    def test_paused_drops_end_timestamp_and_shows_pause_icon(self) -> None:
        raw = read_vlc("Paused", AUDIO_META, position_s=50)
        self.assertEqual(raw.status, "Paused")
        self.assertIsNone(raw.end_at)
        self.assertIsNone(raw.start_at)

        presence = resolve(make_engine(), to_media_info(raw)).presence
        self.assertIsNone(presence.end)
        self.assertIsNone(presence.start)
        self.assertEqual(presence.small_image, "pause")
        self.assertEqual(presence.small_text, "Dijeda")
        self.assertEqual(presence.details, "[Paused] Sialan")
        self.assertNotIn("end", presence.to_kwargs())

    def test_stopped_vlc_is_released(self) -> None:
        bus = make_fake_bus({VLC: vlc_props("Stopped", {})})
        self.assertIsNone(LinuxMprisExtractor(bus_factory=lambda: bus, vlc_client=NO_VLC_HTTP)._read_session())

    def test_live_stream_without_length_uses_running_time_only(self) -> None:
        meta = {"xesam:title": "Radio Stream", "xesam:url": "http://radio.example/live", "mpris:length": 0}
        before = time.time()
        raw = read_vlc("Playing", meta, position_s=30)
        self.assertEqual(raw.duration, 0.0)
        self.assertIsNone(raw.end_at)
        self.assertAlmostEqual(raw.start_at, before - 30.0, delta=2.0)
        presence = resolve(make_engine(), to_media_info(raw)).presence
        self.assertIsNotNone(presence.start)
        self.assertIsNone(presence.end)

    def test_missing_title_falls_back_to_file_name(self) -> None:
        meta = {"xesam:url": "file:///v/My%20Holiday%20%5B1080p%5D.mp4", "mpris:length": 60_000_000}
        self.assertEqual(read_vlc("Playing", meta).title, "My Holiday")


class VlcPlayerSelectionTests(unittest.TestCase):
    def test_playing_vlc_beats_other_playing_player(self) -> None:
        bus = make_fake_bus(
            {
                SPOTIFY: {"PlaybackStatus": "Playing", "Metadata": {"xesam:title": "Other"}, "Position": 0},
                VLC: vlc_props("Playing", AUDIO_META),
            }
        )
        raw = LinuxMprisExtractor(preferred_apps=["spotify"], bus_factory=lambda: bus, vlc_client=NO_VLC_HTTP)._read_session()
        self.assertEqual(raw.app_id, "vlc")

    def test_paused_vlc_only_shows_when_nothing_else_plays(self) -> None:
        bus = make_fake_bus(
            {
                SPOTIFY: {"PlaybackStatus": "Playing", "Metadata": {"xesam:title": "Other"}, "Position": 0},
                VLC: vlc_props("Paused", AUDIO_META),
            }
        )
        self.assertEqual(LinuxMprisExtractor(bus_factory=lambda: bus, vlc_client=NO_VLC_HTTP)._read_session().app_id, "spotify")

    def test_pause_and_resume_publish_distinct_states(self) -> None:
        from extractors.mood_engine import MOOD_ENGINE

        props = vlc_props("Playing", AUDIO_META, 10)
        bus = make_fake_bus({VLC: props})
        resolver = mock.MagicMock()
        resolver.get_cached.return_value = (True, "")
        extractor = LinuxMprisExtractor(bus_factory=lambda: bus, vlc_client=NO_VLC_HTTP, art_resolver=resolver)
        emitted: list = []
        extractor.media_changed.connect(emitted.append)
        with mock.patch.object(MOOD_ENGINE, "enabled", False):
            extractor._poll_once()
            extractor._poll_once()  # unchanged: silent
            props["PlaybackStatus"] = "Paused"
            extractor._poll_once()
            props["PlaybackStatus"] = "Playing"
            extractor._poll_once()
        self.assertEqual([m.status for m in emitted], ["Playing", "Paused", "Playing"])
        self.assertIsNone(emitted[1].end_at)


class VlcSanitizerTests(unittest.TestCase):
    def test_release_tags_and_extensions_are_removed(self) -> None:
        cases = {
            "Inception (2010) [1080p] [x265] [Subs].mkv": "Inception (2010)",
            "[SubsPlease] Frieren - 05 (1080p) [ABCD1234].mkv": "Frieren - 05",
            "Interstellar.2014.2160p.UHD.BluRay.x265.10bit.HDR.mp4": "Interstellar 2014",
            "My_Home_Video.avi": "My Home Video",
            "Song Title.mp3": "Song Title",
            "Plain Title": "Plain Title",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(sanitize_media_title(raw), expected)

    def test_kind_classification(self) -> None:
        for ext in ("mp3", "flac", "wav", "m4a"):
            self.assertEqual(classify_media_kind("x", f"file:///a/b.{ext}"), "audio")
        for ext in ("mp4", "mkv", "avi", "webm"):
            self.assertEqual(classify_media_kind("x", f"file:///a/b.{ext}"), "video")
        self.assertEqual(classify_media_kind("untagged", "", artist="Someone"), "audio")
        self.assertEqual(classify_media_kind("untagged"), "video")


class VlcPriorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = make_engine()
        self.vlc = to_media_info(read_vlc("Playing", AUDIO_META, 5))

    def test_vlc_is_labelled_p5_but_overrides_p4_and_p3(self) -> None:
        self.assertEqual(self.engine.rulebook.get("vlc-audio").tier, Tier.MEDIA)
        for label, activity in (("P4 editor", EDITOR), ("P3 game", GAME)):
            with self.subTest(label):
                result = resolve(self.engine, self.vlc, [activity])
                self.assertEqual(result.rule.id, "vlc-audio")

    def test_paused_vlc_still_overrides_lower_priorities(self) -> None:
        paused = to_media_info(read_vlc("Paused", AUDIO_META, 5))
        result = resolve(self.engine, paused, [EDITOR, GAME])
        self.assertEqual(result.rule.id, "vlc-audio")
        self.assertEqual(result.presence.small_image, "pause")

    def test_manual_override_still_wins(self) -> None:
        result = resolve(self.engine, self.vlc, [EDITOR], manual=("Custom", "status"))
        self.assertEqual(result.rule.id, "manual-override")

    def test_stop_falls_back_to_next_priority(self) -> None:
        self.assertEqual(resolve(self.engine, self.vlc, [EDITOR]).rule.id, "vlc-audio")
        self.engine.clear_media_grace()
        self.assertEqual(resolve(self.engine, None, [EDITOR, GAME]).rule.id, "game-known")
        self.assertEqual(resolve(self.engine, None, [EDITOR]).rule.id, "vscode")
        self.assertEqual(resolve(self.engine, None, []).rule.id, "idle-standby")

    def test_non_vlc_media_does_not_override_editor(self) -> None:
        spotify = MediaInfo("spotify", "Track", "Artist", "", 100.0, None, None)
        self.assertEqual(resolve(self.engine, spotify, [EDITOR]).rule.id, "vscode")


class VlcResumeRegressionTests(unittest.TestCase):
    def _extractor(self, props: dict):
        from extractors.mood_engine import MOOD_ENGINE

        bus = make_fake_bus({VLC: props})
        resolver = mock.MagicMock()
        resolver.get_cached.return_value = (True, "")
        extractor = LinuxMprisExtractor(bus_factory=lambda: bus, vlc_client=NO_VLC_HTTP, art_resolver=resolver)
        emitted: list = []
        extractor.media_changed.connect(emitted.append)
        patcher = mock.patch.object(MOOD_ENGINE, "enabled", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        return extractor, emitted

    def test_playing_paused_playing_always_emits_with_fresh_timestamps(self) -> None:
        props = vlc_props("Playing", AUDIO_META, 10)
        extractor, emitted = self._extractor(props)
        engine = make_engine()

        extractor._poll_once()
        props["PlaybackStatus"], props["Position"] = "Paused", 40_000_000
        extractor._poll_once()
        props["PlaybackStatus"], props["Position"] = "Playing", 40_000_000
        before = time.time()
        extractor._poll_once()

        self.assertEqual([m.status for m in emitted], ["Playing", "Paused", "Playing"])
        playing, paused, resumed = emitted
        self.assertIsNone(paused.start_at)
        self.assertIsNone(paused.end_at)
        self.assertIsNotNone(resumed.start_at)
        self.assertAlmostEqual(resumed.start_at, before - 40.0, delta=2.0)
        self.assertAlmostEqual(resumed.end_at - resumed.start_at, 200.0)

        shown = [resolve(engine, info).presence for info in emitted]
        self.assertTrue(all(p is not None and p.details for p in shown))
        self.assertIsNone(shown[1].end)
        self.assertIsNotNone(shown[2].start)
        self.assertIsNotNone(shown[2].end)
        self.assertEqual(shown[2].small_image, "playing")
        self.assertNotEqual(shown[1], shown[2])  # IPC must see a different payload and push it

    def test_seek_while_playing_publishes_a_dirty_update(self) -> None:
        props = vlc_props("Playing", AUDIO_META, 10)
        extractor, emitted = self._extractor(props)
        extractor._poll_once()
        extractor._poll_once()
        self.assertEqual(len(emitted), 1)
        props["Position"] = 120_000_000
        extractor._poll_once()
        self.assertEqual(len(emitted), 2)
        self.assertEqual(emitted[1].status, "Playing")

    def test_stale_smtc_timeline_does_not_overshoot_after_resume(self) -> None:
        from datetime import datetime, timedelta, timezone

        key = ("vlc.exe", "Movie")
        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        anchor, pos = anchored_position(None, key, "Paused", 100.0, t0, t0)
        self.assertEqual(pos, 100.0)
        # Resume 10 minutes later; SMTC still reports the old position and last_updated.
        t1 = t0 + timedelta(minutes=10)
        anchor, pos = anchored_position(anchor, key, "Playing", 100.0, t0, t1)
        self.assertEqual(pos, 100.0)
        anchor, pos = anchored_position(anchor, key, "Playing", 100.0, t0, t1 + timedelta(seconds=5))
        self.assertEqual(pos, 105.0)
        # Once SMTC refreshes its timeline the anchor is released.
        t2 = t1 + timedelta(seconds=10)
        anchor, pos = anchored_position(anchor, key, "Playing", 112.0, t2, t2 + timedelta(seconds=1))
        self.assertIsNone(anchor)
        self.assertEqual(pos, 113.0)


class P5OrderTests(unittest.TestCase):
    YOUTUBE = Activity("browser-youtube", "yt", {"video": "Some Video", "app_label": "Chrome"})

    def setUp(self) -> None:
        from extractors.youtube_resolver import YOUTUBE_THUMBNAIL_RESOLVER

        patcher = mock.patch.object(YOUTUBE_THUMBNAIL_RESOLVER, "resolve", return_value="")  # no network
        patcher.start()
        self.addCleanup(patcher.stop)

    def _engine(self, reverse: bool = False) -> PriorityEngine:
        book = RuleBook.load(ROOT / "config" / "rules.json").with_enabled("browser-youtube", True).with_enabled("spotify", True)
        if reverse:  # the engine must not depend on rules.json order
            book = RuleBook(list(reversed(book.rules)), book.process_labels, book.languages)
        return PriorityEngine(
            book, 10_000, True, False, mood_engine_enabled=False, celestial_sync_enabled=False,
            privacy_show_city=False, idle_custom_image="",
        )

    def test_rules_json_lists_p5_in_required_order(self) -> None:
        ids = [r.id for r in RuleBook.load(ROOT / "config" / "rules.json").rules if r.tier == Tier.MEDIA]
        wanted = ["vlc-audio", "vlc-video", "browser-youtube", "spotify", "apple-music", "media-generic"]
        self.assertEqual(sorted(ids[:2]), ["vlc-audio", "vlc-video"])
        self.assertEqual(ids[2:], wanted[2:])

    def test_order_vlc_youtube_spotify_other(self) -> None:
        spotify = MediaInfo("spotify", "Track", "Artist", "", 100.0, None, None)
        apple = MediaInfo("applemusic", "Track", "Artist", "", 100.0, None, None)
        vlc_playing = to_media_info(read_vlc("Playing", AUDIO_META, 5))
        vlc_paused = to_media_info(read_vlc("Paused", AUDIO_META, 5))
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                engine = self._engine(reverse)
                self.assertEqual(resolve(engine, vlc_playing, [self.YOUTUBE]).rule.id, "vlc-audio")
                self.assertEqual(resolve(engine, vlc_paused, [self.YOUTUBE]).rule.id, "vlc-audio")
                self.assertEqual(resolve(engine, spotify, [self.YOUTUBE]).rule.id, "browser-youtube")
                self.assertEqual(resolve(engine, apple, [self.YOUTUBE]).rule.id, "browser-youtube")
                self.assertEqual(resolve(engine, spotify, []).rule.id, "spotify")
                self.assertEqual(resolve(engine, None, [self.YOUTUBE]).rule.id, "browser-youtube")

    def test_vlc_stopped_falls_back_to_youtube(self) -> None:
        engine = self._engine()
        vlc = to_media_info(read_vlc("Playing", AUDIO_META, 5))
        self.assertEqual(resolve(engine, vlc, [self.YOUTUBE]).rule.id, "vlc-audio")
        engine.clear_media_grace()
        self.assertEqual(resolve(engine, None, [self.YOUTUBE]).rule.id, "browser-youtube")


class VersionTests(unittest.TestCase):
    def test_version_is_2_4_1_everywhere(self) -> None:
        import re

        from core.updater import CURRENT_VERSION
        from core.version import APP_VERSION, RELEASE_TAG

        self.assertEqual(APP_VERSION, "2.4.1")
        self.assertEqual(RELEASE_TAG, "OmniPresence@2.4.1")
        self.assertEqual(CURRENT_VERSION, "v2.4.1")
        build = (ROOT / "build_exe.py").read_text(encoding="utf-8")
        self.assertIn('APP_VERSION = "2.4.1"', build)
        self.assertIn("(2, 4, 1, 0)", build)
        self.assertRegex((ROOT / "installer" / "installer.iss").read_text(encoding="utf-8"), r'MyAppVersion "2\.4\.1"')
        self.assertIsNotNone(re.search(r"2\.4\.1\.0", (ROOT / "build" / "version_info.txt").read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()

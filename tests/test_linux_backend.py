"""Hermetic tests for the Linux backend (MPRIS2 over D-Bus, /proc telemetry). Runs on Windows via mocks."""
from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QCoreApplication

from extractors.media.base import RawTrack
from extractors.media.linux_mpris import MPRIS_PATH, PLAYER_IFACE, LinuxMprisExtractor
from extractors.media.vlc_http import VlcHttpClient, VlcHttpConfig
from extractors.telemetry.linux_telemetry import (
    LinuxTelemetry,
    parse_meminfo,
    parse_proc_stat,
)

ROOT = Path(__file__).resolve().parents[1]
NO_VLC_HTTP = VlcHttpClient(lambda: VlcHttpConfig(enabled=False))  # hermetic: never probe localhost:8080

SPOTIFY_TRACK_ID = "4uLU6hMCjMI75M1A2tKUQC"
SPOTIFY_METADATA = {
    "mpris:trackid": f"/com/spotify/track/{SPOTIFY_TRACK_ID}",
    "mpris:length": 215_000_000,
    "mpris:artUrl": "https://i.scdn.co/image/ab67616d0000b273abcdef",
    "xesam:title": "Sialan",
    "xesam:artist": ["Juicy Luicy", "Adrian Khalif"],
    "xesam:album": "Nonfiksi",
    "xesam:url": f"https://open.spotify.com/track/{SPOTIFY_TRACK_ID}",
}

PROC_STAT_1 = "cpu  1000 0 500 8000 500 0 0 0 0 0\ncpu0 500 0 250 4000 250 0 0 0 0 0\nintr 12345\n"
PROC_STAT_2 = "cpu  2000 0 1000 8900 600 0 500 0 0 0\ncpu0 1000 0 500 4450 300 0 250 0 0 0\nintr 23456\n"
MEMINFO = (
    "MemTotal:       16384000 kB\n"
    "MemFree:         2000000 kB\n"
    "MemAvailable:    8192000 kB\n"
    "Buffers:          500000 kB\n"
    "Cached:          4000000 kB\n"
)


def setUpModule() -> None:
    QCoreApplication.instance() or QCoreApplication([])


def make_fake_bus(players: dict[str, dict]) -> mock.MagicMock:
    """Build a MagicMock session bus exposing org.mpris.MediaPlayer2.* proxies."""
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


def spotify_player(status: str = "Playing") -> dict:
    return {"PlaybackStatus": status, "Metadata": SPOTIFY_METADATA, "Position": 30_000_000}


class LinuxMprisExtractorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = make_fake_bus(
            {
                "org.mpris.MediaPlayer2.spotify": spotify_player("Playing"),
                "org.mpris.MediaPlayer2.vlc": {
                    "PlaybackStatus": "Paused",
                    "Metadata": {"xesam:title": "Ignored"},
                    "Position": 0,
                },
            }
        )
        self.extractor = LinuxMprisExtractor(preferred_apps=["spotify"], bus_factory=lambda: self.bus, vlc_client=NO_VLC_HTTP)

    def test_extracts_title_artist_album_and_cover(self) -> None:
        before = time.time()
        raw = self.extractor._read_session()
        self.assertIsInstance(raw, RawTrack)
        self.assertEqual(raw.app_id, "spotify")
        self.assertEqual(raw.title, "Sialan")
        self.assertEqual(raw.artist, "Juicy Luicy")
        self.assertEqual(raw.album, "Nonfiksi")
        self.assertEqual(raw.art_url, "https://i.scdn.co/image/ab67616d0000b273abcdef")
        self.assertEqual(raw.track_url, f"https://open.spotify.com/track/{SPOTIFY_TRACK_ID}")
        self.assertAlmostEqual(raw.duration, 215.0)
        self.assertAlmostEqual(raw.start_at, before - 30.0, delta=2.0)
        self.assertAlmostEqual(raw.end_at - raw.start_at, 215.0)
        self.bus.get_object.assert_any_call("org.mpris.MediaPlayer2.spotify", MPRIS_PATH)

    def test_queries_player_interface_properties(self) -> None:
        self.extractor._read_session()
        proxy = self.bus.get_object("org.mpris.MediaPlayer2.spotify", MPRIS_PATH)
        requested = {call.args[1] for call in proxy.Get.call_args_list}
        self.assertTrue({"PlaybackStatus", "Metadata", "Position"} <= requested)
        self.assertTrue(all(call.args[0] == PLAYER_IFACE for call in proxy.Get.call_args_list))

    def test_returns_none_when_nothing_is_playing(self) -> None:
        bus = make_fake_bus({"org.mpris.MediaPlayer2.spotify": spotify_player("Paused")})
        extractor = LinuxMprisExtractor(bus_factory=lambda: bus, vlc_client=NO_VLC_HTTP)
        self.assertIsNone(extractor._read_session())

    def test_file_cover_art_is_ignored_for_discord(self) -> None:
        meta = dict(SPOTIFY_METADATA, **{"mpris:artUrl": "file:///tmp/cover.png"})
        raw = LinuxMprisExtractor.parse_metadata("org.mpris.MediaPlayer2.vlc", meta)
        self.assertEqual(raw.art_url, "")

    def test_poll_emits_once_per_track_and_exposes_current_track(self) -> None:
        from extractors.mood_engine import MOOD_ENGINE

        emitted: list = []
        self.extractor.media_changed.connect(emitted.append)
        with mock.patch.object(MOOD_ENGINE, "enabled", False):
            self.extractor._poll_once()
            self.extractor._poll_once()  # same artist+title hash: must stay silent
        self.assertEqual(len(emitted), 1)
        info = emitted[0]
        self.assertEqual((info.title, info.artist, info.album), ("Sialan", "Juicy Luicy", "Nonfiksi"))
        self.assertTrue(info.artwork_url.startswith("https://i.scdn.co/"))
        self.assertIs(self.extractor.get_current_track(), info)

    def test_stop_needs_two_consecutive_empty_ticks(self) -> None:
        from extractors.mood_engine import MOOD_ENGINE

        emitted: list = []
        self.extractor.media_changed.connect(emitted.append)
        with mock.patch.object(MOOD_ENGINE, "enabled", False):
            self.extractor._poll_once()
            self.bus.list_names.return_value = ["org.freedesktop.DBus"]
            self.extractor._poll_once()
            self.assertEqual(len(emitted), 1, "first empty tick must not emit")
            self.extractor._poll_once()
        self.assertEqual(len(emitted), 2)
        self.assertIsNone(emitted[-1])
        self.assertIsNone(self.extractor.get_current_track())


class LinuxTelemetryTests(unittest.TestCase):
    @staticmethod
    def _fake_open(files: dict[str, str]):
        def opener(path, *args, **kwargs):
            return mock.mock_open(read_data=files[str(path)])()

        return opener

    def test_cpu_delta_and_ram_from_proc(self) -> None:
        files = {"/proc/stat": PROC_STAT_1, "/proc/meminfo": MEMINFO}
        with mock.patch("builtins.open", side_effect=self._fake_open(files)):
            telemetry = LinuxTelemetry()
            files["/proc/stat"] = PROC_STAT_2
            sample = telemetry.sample()
        # d_total = 13000 - 10000, d_idle = 9500 - 8500  ->  CPU = (1 - 1000/3000) * 100
        self.assertAlmostEqual(sample.cpu_percent, 66.6667, places=3)
        self.assertAlmostEqual(sample.ram_percent, 50.0)
        self.assertAlmostEqual(sample.ram_used_gb, 8192000 * 1024 / 1024**3)
        self.assertAlmostEqual(sample.ram_total_gb, 16384000 * 1024 / 1024**3)
        self.assertEqual(sample.as_variables()["cpu_percent"], "67")
        self.assertEqual(sample.as_variables()["ram_used_gb"], "7.8")
        self.assertEqual(sample.as_variables()["ram_total_gb"], "16")

    def test_parse_proc_stat_totals(self) -> None:
        self.assertEqual(parse_proc_stat(PROC_STAT_1), (10000, 8500))
        self.assertEqual(parse_proc_stat(PROC_STAT_2), (13000, 9500))
        with self.assertRaises(ValueError):
            parse_proc_stat("intr 1 2 3\n")

    def test_meminfo_falls_back_without_memavailable(self) -> None:
        text = "MemTotal: 1000 kB\nMemFree: 100 kB\nBuffers: 100 kB\nCached: 300 kB\n"
        used, total = parse_meminfo(text)
        self.assertEqual(total, 1000 * 1024)
        self.assertEqual(used, 500 * 1024)

    def test_first_reading_without_baseline_is_zero_not_a_crash(self) -> None:
        files = {"/proc/stat": PROC_STAT_1, "/proc/meminfo": MEMINFO}
        with mock.patch("builtins.open", side_effect=self._fake_open(files)):
            telemetry = LinuxTelemetry()
            telemetry._prev = None
            self.assertEqual(telemetry.sample().cpu_percent, 0.0)


@unittest.skipUnless(sys.platform.startswith("win"), "Windows-only guard for lazy platform imports")
class WindowsImportIsolationTests(unittest.TestCase):
    def test_windows_never_imports_dbus_or_linux_backends(self) -> None:
        code = (
            "import sys, extractors.media as m, extractors.telemetry as t;"
            "m.get_extractor_class(); t.get_sampler_class();"
            "assert 'dbus' not in sys.modules, 'dbus imported on Windows';"
            "assert 'extractors.media.linux_mpris' not in sys.modules;"
            "assert 'extractors.telemetry.linux_telemetry' not in sys.modules;"
            "print('isolated')"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("isolated", result.stdout)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for pure event-driven Discord IPC emission, 250ms VLC polling, and immediate Play/Pause flush."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication

from core.ipc_manager import IPCManager
from core.priority_engine import RuleBook
from core.settings_manager import Settings, VLC_CLIENT_ID
from core.state_manager import StateManager
from extractors.media.base import BaseMediaExtractor
from extractors.media.common import SEEK_TOLERANCE_SECONDS
from extractors.media.poster_resolver import POSTER_RESOLVER
from extractors.media.vlc_http import POLL_INTERVAL_MS, VlcHttpClient, VlcHttpConfig
from extractors.media.windows_smtc import WindowsSmtcExtractor

ROOT = Path(__file__).resolve().parents[1]


def setUpModule() -> None:
    QApplication.instance() or QApplication([])


class _FakeWriter:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeReader:
    def at_eof(self) -> bool:
        return False


class _FakeRPC:
    """Mock Discord IPC socket capturing every RPC update, clear, and connection lifecycle event."""

    instances: list["_FakeRPC"] = []

    def __init__(self, client_id: str, loop=None) -> None:
        self.client_id = client_id
        self.loop = loop
        self.ready_user = {"id": "1001", "username": "raf", "global_name": "Raf"}
        self.connected = False
        self.cleared = False
        self.updates: list[dict] = []
        self.sock_writer = _FakeWriter()
        self.sock_reader = _FakeReader()
        _FakeRPC.instances.append(self)

    async def connect(self) -> None:
        self.connected = True

    async def update(self, **kwargs) -> None:
        self.updates.append(kwargs)

    async def clear(self) -> None:
        self.cleared = True


class _ScriptedVlcExtractor(BaseMediaExtractor):
    """Extractor backed by a scripted VlcHttpClient for deterministic event-driven pipeline tests."""

    POLL_INTERVAL_MS = POLL_INTERVAL_MS

    def __init__(self, vlc_client: VlcHttpClient) -> None:
        art_resolver = MagicMock()
        art_resolver.get_cached.return_value = (True, "")
        super().__init__(art_resolver=art_resolver)
        self._vlc = vlc_client

    def _read_session(self):
        return self._vlc.fetch()


def _vlc_status_payload(state: str, time_s: float, length_s: float = 1440.0, filename: str = "Clannad BD - 01.mkv") -> dict:
    return {
        "state": state,
        "time": time_s,
        "length": length_s,
        "information": {
            "category": {
                "meta": {
                    "filename": filename,
                    "title": filename,
                },
                "Stream 0": {"Type": "Video"},
            }
        },
    }


class TestFastVlcPollingConfig(unittest.TestCase):
    """Verify VLC HTTP polling interval is configured to 250ms and seek tolerance is 2.0s."""

    def test_poll_interval_is_250ms_and_seek_tolerance_is_2s(self) -> None:
        self.assertEqual(POLL_INTERVAL_MS, 250)
        self.assertEqual(WindowsSmtcExtractor.POLL_INTERVAL_MS, 250)
        self.assertEqual(SEEK_TOLERANCE_SECONDS, 2.0)
        self.assertFalse(BaseMediaExtractor.is_manual_seek(11.0, 10.0))
        self.assertFalse(BaseMediaExtractor.is_manual_seek(12.0, 10.0))
        self.assertTrue(BaseMediaExtractor.is_manual_seek(150.0, 10.0))


class TestEventDrivenIpcPipeline(unittest.TestCase):
    """Verify end-to-end event-driven IPC behavior: 15s steady playback, immediate pause/resume, and manual seek."""

    def setUp(self) -> None:
        _FakeRPC.instances.clear()
        POSTER_RESOLVER.clear_cache()
        self.poster_url = "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/bx2167-clannad.jpg"
        POSTER_RESOLVER.cache["clannad"] = self.poster_url
        POSTER_RESOLVER._expires_at["clannad"] = 1e18

        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        rules_src = ROOT / "config" / "rules.json"
        rules_dst = root / "rules.json"
        rules_dst.write_text(rules_src.read_text(encoding="utf-8"), encoding="utf-8")

        self.settings = Settings.load(root / "settings.json")
        self.settings.mood_engine_enabled = False
        self.settings.celestial_sync_enabled = False
        self.rules = RuleBook.load(rules_dst).with_enabled("vlc-video", True)
        self.state = StateManager(self.settings, rules_dst, self.rules)

    def tearDown(self) -> None:
        POSTER_RESOLVER.clear_cache()
        self._tmpdir.cleanup()

    def test_15_seconds_steady_playback_sends_only_1_ipc_payload(self) -> None:
        """Simulate 15 consecutive seconds of normal video playback: IPC socket must receive ONLY 1 initial payload."""
        clock = {"now": 1_800_000_000.0}
        payloads: list[dict] = []
        # 16 ticks spanning t=0..15s (60 ticks at 250ms or 16 ticks at 1s), where playback advances linearly 10s..25s
        for step in range(61):
            elapsed = step * 0.25
            # VLC reports integer seconds in status.json
            vlc_time_int = int(10 + elapsed)
            payloads.append(_vlc_status_payload("playing", vlc_time_int, 1440.0))

        def fake_request_json(_cfg):
            return payloads.pop(0)

        client = VlcHttpClient(config_provider=lambda: VlcHttpConfig(enabled=True), clock=lambda: clock["now"])
        client._request_json = fake_request_json  # type: ignore[method-assign]
        extractor = _ScriptedVlcExtractor(client)

        ipc = IPCManager(client_id=VLC_CLIENT_ID)
        extractor.media_changed.connect(self.state.on_media)
        self.state.client_id_changed.connect(ipc.set_client_id)
        self.state.outgoing_changed.connect(ipc.submit)

        async def run_scenario() -> None:
            ipc._loop = asyncio.get_running_loop()
            main_task = asyncio.create_task(ipc._main())
            await asyncio.sleep(0.05)

            # Simulate 15 full seconds of 250ms polls (61 ticks = 15.0s of continuous linear playback)
            for _ in range(61):
                extractor._poll_once()
                clock["now"] += 0.25
                await asyncio.sleep(0.002)

            await asyncio.sleep(0.05)

            # Socket must remain connected on the single VLC Client ID instance with NO re-handshakes
            self.assertEqual(len(_FakeRPC.instances), 1)
            rpc = _FakeRPC.instances[0]
            self.assertEqual(rpc.client_id, VLC_CLIENT_ID)
            self.assertFalse(rpc.sock_writer.closed)

            # Exactly 1 payload must have been sent over the entire 15 seconds of normal playback
            self.assertEqual(len(rpc.updates), 1)
            first_update = rpc.updates[0]
            self.assertEqual(first_update["large_image"], self.poster_url)
            self.assertEqual(first_update["small_image"], "vlc")
            self.assertIsNotNone(first_update.get("start"))
            self.assertIsNotNone(first_update.get("end"))

            ipc._halt.set()
            ipc._poke()
            await asyncio.wait_for(main_task, timeout=1.0)

        with patch("core.ipc_manager._HandshakeAwarePresence", _FakeRPC):
            asyncio.run(run_scenario())

    def test_pause_and_resume_flush_immediately_and_preserve_socket(self) -> None:
        """Simulate Spacebar pause & resume: must flush immediately with large_image='pause' without closing socket."""
        clock = {"now": 1_800_000_000.0}
        script = [
            _vlc_status_payload("playing", 10, 1440.0),
            _vlc_status_payload("paused", 25, 1440.0),
            _vlc_status_payload("playing", 25, 1440.0),
        ]

        client = VlcHttpClient(config_provider=lambda: VlcHttpConfig(enabled=True), clock=lambda: clock["now"])
        client._request_json = lambda _cfg: script.pop(0)  # type: ignore[method-assign]
        extractor = _ScriptedVlcExtractor(client)

        ipc = IPCManager(client_id=VLC_CLIENT_ID)
        extractor.media_changed.connect(self.state.on_media)
        self.state.client_id_changed.connect(ipc.set_client_id)
        self.state.outgoing_changed.connect(ipc.submit)

        async def run_scenario() -> None:
            ipc._loop = asyncio.get_running_loop()
            main_task = asyncio.create_task(ipc._main())
            await asyncio.sleep(0.04)

            # 1. Initial play at 00:10
            extractor._poll_once()
            await asyncio.sleep(0.03)

            rpc = _FakeRPC.instances[0]
            self.assertEqual(len(rpc.updates), 1)
            self.assertEqual(rpc.updates[0]["large_image"], self.poster_url)

            # 2. User presses Spacebar (Paused at 00:25) -> immediate flush without interval delay
            clock["now"] += 15.0
            extractor._poll_once()
            await asyncio.sleep(0.03)

            self.assertEqual(len(_FakeRPC.instances), 1, "Socket must NOT disconnect/re-handshake on pause")
            self.assertFalse(rpc.sock_writer.closed)
            self.assertEqual(len(rpc.updates), 2)
            paused_payload = rpc.updates[1]
            self.assertEqual(paused_payload["large_image"], "pause")
            self.assertEqual(paused_payload["large_text"], "Dijeda - Clannad BD - 01")
            self.assertIsNone(paused_payload.get("start"))
            self.assertIsNone(paused_payload.get("end"))

            # 3. User presses Spacebar again (Resume Playing at 00:25 after 10s pause) -> immediate flush
            clock["now"] += 10.0
            extractor._poll_once()
            await asyncio.sleep(0.03)

            self.assertEqual(len(_FakeRPC.instances), 1, "Socket must NOT disconnect/re-handshake on resume")
            self.assertEqual(len(rpc.updates), 3)
            resumed_payload = rpc.updates[2]
            self.assertEqual(resumed_payload["large_image"], self.poster_url)
            self.assertEqual(resumed_payload["small_image"], "vlc")
            expected_start = int(clock["now"] - 25)
            expected_end = int(expected_start + 1440)
            self.assertAlmostEqual(resumed_payload["start"], expected_start, delta=1)
            self.assertAlmostEqual(resumed_payload["end"], expected_end, delta=1)

            ipc._halt.set()
            ipc._poke()
            await asyncio.wait_for(main_task, timeout=1.0)

        with patch("core.ipc_manager._HandshakeAwarePresence", _FakeRPC):
            asyncio.run(run_scenario())

    def test_manual_seek_from_00_10_to_02_30_triggers_new_timestamp_calculation(self) -> None:
        """Simulate jumping from 00:10 (10s) to 02:30 (150s): seek > 2.0s must immediately emit updated timestamps."""
        clock = {"now": 1_800_000_000.0}
        script = [
            _vlc_status_payload("playing", 10, 1440.0),   # 00:10
            _vlc_status_payload("playing", 11, 1440.0),   # 00:11 (linear progression -> ignored)
            _vlc_status_payload("playing", 150, 1440.0),  # 02:30 (manual seek jump of +138s > 2.0s -> emitted!)
        ]

        client = VlcHttpClient(config_provider=lambda: VlcHttpConfig(enabled=True), clock=lambda: clock["now"])
        client._request_json = lambda _cfg: script.pop(0)  # type: ignore[method-assign]
        extractor = _ScriptedVlcExtractor(client)

        ipc = IPCManager(client_id=VLC_CLIENT_ID)
        extractor.media_changed.connect(self.state.on_media)
        self.state.client_id_changed.connect(ipc.set_client_id)
        self.state.outgoing_changed.connect(ipc.submit)

        async def run_scenario() -> None:
            ipc._loop = asyncio.get_running_loop()
            main_task = asyncio.create_task(ipc._main())
            await asyncio.sleep(0.04)

            # Tick 1: 00:10 at t=0
            extractor._poll_once()
            await asyncio.sleep(0.03)
            rpc = _FakeRPC.instances[0]
            self.assertEqual(len(rpc.updates), 1)
            initial_start = rpc.updates[0]["start"]

            # Tick 2: 00:11 at t=1 (linear playback -> no IPC emission)
            clock["now"] += 1.0
            extractor._poll_once()
            await asyncio.sleep(0.03)
            self.assertEqual(len(rpc.updates), 1)

            # Tick 3: User seeks to 02:30 (150s) at t=2 -> immediate IPC emission with new start/end
            clock["now"] += 1.0
            extractor._poll_once()
            await asyncio.sleep(0.03)
            self.assertEqual(len(rpc.updates), 2)
            seek_start = rpc.updates[1]["start"]
            seek_end = rpc.updates[1]["end"]
            expected_seek_start = int(clock["now"] - 150)
            self.assertAlmostEqual(seek_start, expected_seek_start, delta=1)
            self.assertAlmostEqual(seek_end - seek_start, 1440, delta=1)
            self.assertGreater(abs(initial_start - seek_start), 130)

            ipc._halt.set()
            ipc._poke()
            await asyncio.wait_for(main_task, timeout=1.0)

        with patch("core.ipc_manager._HandshakeAwarePresence", _FakeRPC):
            asyncio.run(run_scenario())

    def test_paused_strict_deduplication_and_no_console_spam(self) -> None:
        """Repeatedly re-evaluating or submitting identical Paused state must NOT re-send or log 'presence sent' every second."""
        from extractors.media.common import MediaInfo

        ipc = IPCManager(client_id=VLC_CLIENT_ID)
        self.assertEqual(ipc.HEARTBEAT_INTERVAL, 40.0)
        self.state.client_id_changed.connect(ipc.set_client_id)
        self.state.outgoing_changed.connect(ipc.submit)
        ipc.presence_sent.connect(self.state.on_presence_sent)

        paused_media = MediaInfo(
            app_id="vlc",
            title="Clannad BD - 01",
            artist="",
            album="",
            duration=1440.0,
            start_at=None,
            end_at=None,
            status="Paused",
            kind="video",
        )

        async def run_scenario() -> None:
            ipc._loop = asyncio.get_running_loop()
            main_task = asyncio.create_task(ipc._main())
            await asyncio.sleep(0.04)

            # Simulate 20 consecutive 1-second ticks while media is Paused (including forced re-evaluations)
            for _ in range(20):
                self.state.on_media(paused_media)
                self.state._reevaluate()
                if self.state._last_outgoing is not None:
                    ipc.submit(self.state._last_outgoing)
                await asyncio.sleep(0.005)

            await asyncio.sleep(0.05)

            rpc = _FakeRPC.instances[0]
            # Strict deduplication: only 1 paused payload may be sent to Discord
            self.assertEqual(len(rpc.updates), 1)
            self.assertEqual(rpc.updates[0]["large_image"], "pause")

            # Activity Console must NOT have repeated 'presence sent' logs every second
            sent_logs = [entry.text for entry in self.state.log_history if entry.text.startswith("presence sent:")]
            self.assertEqual(len(sent_logs), 1)

            ipc._halt.set()
            ipc._poke()
            await asyncio.wait_for(main_task, timeout=1.0)

        with patch("core.ipc_manager._HandshakeAwarePresence", _FakeRPC):
            asyncio.run(run_scenario())


if __name__ == "__main__":
    unittest.main()

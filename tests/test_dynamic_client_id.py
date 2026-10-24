"""Unit tests for Dynamic Multi-Client ID Switching, IPC Re-Handshake, and Settings UI cleanup."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from core.ipc_manager import IPCManager
from core.priority_engine import Activity, PresenceData, RuleBook
from core.settings_manager import (
    DEFAULT_CLIENT_ID,
    SPOTIFY_CLIENT_ID,
    VLC_CLIENT_ID,
    YOUTUBE_CLIENT_ID,
    Settings,
    resolve_client_id,
)
from core.state_manager import StateManager
from extractors.media.common import MediaInfo


def setUpModule() -> None:
    QApplication.instance() or QApplication([])


def _make_presence(
    *,
    details: str,
    state: str,
    large_image: str,
    large_text: str,
    activity_type: int,
    rule_id: str,
    tier: int,
) -> PresenceData:
    return PresenceData(
        details=details,
        state=state,
        large_image=large_image,
        large_text=large_text,
        small_image=None,
        small_text=None,
        start=1700000000,
        end=None,
        buttons=(),
        activity_type=activity_type,
        tier=tier,
        rule_id=rule_id,
    )


class _FakeWriter:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeReader:
    def at_eof(self) -> bool:
        return False


class _FakeRPC:
    """Mock _HandshakeAwarePresence that records connects, updates, clears, and socket closes."""

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


class TestDynamicClientIdResolution(unittest.TestCase):
    """Verify internal Client ID mapping for VLC, YouTube, Spotify, and Default activities."""

    def test_client_id_constants_match_specification(self) -> None:
        self.assertEqual(VLC_CLIENT_ID, "1563538304593436712")
        self.assertEqual(YOUTUBE_CLIENT_ID, "1562111104556204112")
        self.assertEqual(SPOTIFY_CLIENT_ID, "1562111027104063568")
        self.assertEqual(DEFAULT_CLIENT_ID, "1560465175188996176")

    def test_resolve_client_id_for_each_rule(self) -> None:
        # VLC Media Player rules
        self.assertEqual(resolve_client_id("vlc-video"), "1563538304593436712")
        self.assertEqual(resolve_client_id("vlc-audio"), "1563538304593436712")
        self.assertEqual(resolve_client_id("vlc"), "1563538304593436712")

        # YouTube rule
        self.assertEqual(resolve_client_id("browser-youtube"), "1562111104556204112")
        self.assertEqual(resolve_client_id("youtube-browser"), "1562111104556204112")

        # Spotify rule
        self.assertEqual(resolve_client_id("spotify"), "1562111027104063568")

        # Default (Coding, Gaming, non-YouTube Browser, Standby/Idle)
        self.assertEqual(resolve_client_id("vscode"), DEFAULT_CLIENT_ID)
        self.assertEqual(resolve_client_id("game-known"), DEFAULT_CLIENT_ID)
        self.assertEqual(resolve_client_id("browser-github"), DEFAULT_CLIENT_ID)
        self.assertEqual(resolve_client_id("idle-standby"), DEFAULT_CLIENT_ID)
        self.assertEqual(resolve_client_id(None), DEFAULT_CLIENT_ID)

    def test_ipc_manager_resolve_client_id_for_presence(self) -> None:
        vlc_presence = _make_presence(
            details="Inception",
            state="Playing in VLC",
            large_image="vlc",
            large_text="VLC Media Player",
            activity_type=3,
            rule_id="vlc-video",
            tier=5,
        )
        yt_presence = _make_presence(
            details="Tech Talk",
            state="Watching on YouTube",
            large_image="youtube",
            large_text="YouTube",
            activity_type=3,
            rule_id="browser-youtube",
            tier=5,
        )
        spotify_presence = _make_presence(
            details="Starboy",
            state="by The Weeknd",
            large_image="spotify",
            large_text="Spotify",
            activity_type=2,
            rule_id="spotify",
            tier=5,
        )
        coding_presence = _make_presence(
            details="Working on main.py",
            state="Workspace: DiscordRpc",
            large_image="vscode",
            large_text="VS Code",
            activity_type=0,
            rule_id="vscode",
            tier=2,
        )

        self.assertEqual(IPCManager.resolve_client_id_for_presence(vlc_presence), VLC_CLIENT_ID)
        self.assertEqual(IPCManager.resolve_client_id_for_presence(yt_presence), YOUTUBE_CLIENT_ID)
        self.assertEqual(IPCManager.resolve_client_id_for_presence(spotify_presence), SPOTIFY_CLIENT_ID)
        self.assertEqual(IPCManager.resolve_client_id_for_presence(coding_presence), DEFAULT_CLIENT_ID)
        self.assertEqual(IPCManager.resolve_client_id_for_presence(None), DEFAULT_CLIENT_ID)


class TestStateManagerDynamicClientIdSwitching(unittest.TestCase):
    """Verify StateManager emits client_id_changed when switching between VLC, YouTube, Spotify, and Coding."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        rules_src = Path(__file__).resolve().parent.parent / "config" / "rules.json"
        rules_dst = root / "rules.json"
        rules_dst.write_text(rules_src.read_text(encoding="utf-8"), encoding="utf-8")

        self.settings = Settings.load(root / "settings.json")
        self.settings.mood_engine_enabled = False
        self.settings.celestial_sync_enabled = False
        self.rules = (
            RuleBook.load(rules_dst)
            .with_enabled("vscode", True)
            .with_enabled("vlc-video", True)
            .with_enabled("browser-youtube", True)
            .with_enabled("spotify", True)
        )
        self._yt_patch = patch(
            "extractors.youtube_resolver.YOUTUBE_THUMBNAIL_RESOLVER.resolve_async",
            return_value="youtube",
        )
        self._yt_patch.start()
        self.state = StateManager(self.settings, rules_dst, self.rules)
        self.emitted_client_ids: list[str] = []
        self.state.client_id_changed.connect(self.emitted_client_ids.append)

    def tearDown(self) -> None:
        self._yt_patch.stop()
        self._tmpdir.cleanup()

    def _scan(self, activities: list[Activity]) -> None:
        telemetry = SimpleNamespace(as_variables=lambda: {"cpu": "10%", "ram": "40%", "temp": "45°C"})
        scan_result = SimpleNamespace(activities=tuple(activities), idle_seconds=0.0, telemetry=telemetry)
        self.state.on_scan(scan_result)

    def test_state_manager_switches_client_id_across_activities(self) -> None:
        vscode_act = Activity("vscode", "proj", {"file": "ipc_manager.py", "workspace": "DiscordRpc"})
        yt_act = Activity("browser-youtube", "yt", {"video": "System Design Interview", "browser": "Chrome"})

        # 1. VLC video playing alongside VS Code & YouTube -> VLC wins P5 and switches Client ID to VLC_CLIENT_ID
        self._scan([vscode_act, yt_act])
        vlc_media = MediaInfo(
            app_id="vlc",
            title="Interstellar",
            artist="",
            album="",
            duration=3600.0,
            start_at=1700000000.0,
            end_at=1700003600.0,
            status="Playing",
            kind="video",
        )
        self.state.on_media(vlc_media)
        self.assertEqual(self.state.active_client_id, VLC_CLIENT_ID)
        self.assertEqual(self.emitted_client_ids[-1], VLC_CLIENT_ID)

        # 2. VLC stops, YouTube is active (and no editor blocking P5) -> switches Client ID to YOUTUBE_CLIENT_ID
        self._scan([yt_act])
        self.state.on_media(None)
        self.assertEqual(self.state.active_client_id, YOUTUBE_CLIENT_ID)
        self.assertEqual(self.emitted_client_ids[-1], YOUTUBE_CLIENT_ID)

        # 3. YouTube closes, Spotify is playing -> switches Client ID to SPOTIFY_CLIENT_ID
        self._scan([])
        spotify_media = MediaInfo(
            app_id="spotify",
            title="Midnight City",
            artist="M83",
            album="Hurry Up, We're Dreaming",
            duration=240.0,
            start_at=1700000000.0,
            end_at=1700000240.0,
            status="Playing",
            kind="audio",
        )
        self.state.on_media(spotify_media)
        self.assertEqual(self.state.active_client_id, SPOTIFY_CLIENT_ID)
        self.assertEqual(self.emitted_client_ids[-1], SPOTIFY_CLIENT_ID)

        # 4. Spotify stops, VS Code is active -> switches back to DEFAULT_CLIENT_ID
        self._scan([vscode_act])
        self.state.on_media(None)
        self.assertEqual(self.state.active_client_id, DEFAULT_CLIENT_ID)
        self.assertEqual(self.emitted_client_ids[-1], DEFAULT_CLIENT_ID)


class TestIPCManagerRehandshake(unittest.TestCase):
    """Simulate IPC socket disconnect, debounce delay, and re-handshake when Client ID changes."""

    def test_rehandshake_on_activity_switch_from_vlc_to_coding(self) -> None:
        _FakeRPC.instances.clear()
        ipc = IPCManager(client_id=DEFAULT_CLIENT_ID)
        ipc.REHANDSHAKE_DEBOUNCE_SECONDS = 0.05

        vlc_presence = _make_presence(
            details="Oppenheimer",
            state="00:10:00 / 03:00:00",
            large_image="vlc",
            large_text="VLC Media Player",
            activity_type=3,
            rule_id="vlc-video",
            tier=5,
        )
        coding_presence = _make_presence(
            details="Working on ipc_manager.py",
            state="Workspace: DiscordRpc",
            large_image="vscode",
            large_text="Visual Studio Code",
            activity_type=0,
            rule_id="vscode",
            tier=2,
        )

        async def run_scenario() -> None:
            ipc._loop = asyncio.get_running_loop()
            # 1. Submit VLC presence before starting main loop
            ipc.submit(vlc_presence)
            self.assertEqual(ipc.client_id, VLC_CLIENT_ID)

            main_task = asyncio.create_task(ipc._main())
            # Allow initial VLC handshake & presence push
            await asyncio.sleep(0.08)

            self.assertEqual(len(_FakeRPC.instances), 1)
            first_rpc = _FakeRPC.instances[0]
            self.assertEqual(first_rpc.client_id, VLC_CLIENT_ID)
            self.assertTrue(first_rpc.connected)
            self.assertEqual(len(first_rpc.updates), 1)
            self.assertEqual(first_rpc.updates[0]["details"], "Oppenheimer")

            # 2. Switch activity from VLC to Coding (VS Code -> DEFAULT_CLIENT_ID)
            ipc.submit(coding_presence)
            self.assertEqual(ipc.client_id, DEFAULT_CLIENT_ID)

            # Wait for graceful close + debounce + new handshake + presence push
            await asyncio.sleep(0.15)

            self.assertEqual(len(_FakeRPC.instances), 2)
            # Old socket must be gracefully cleared & closed
            self.assertTrue(first_rpc.cleared)
            self.assertTrue(first_rpc.sock_writer.closed)

            # New socket must handshake with DEFAULT_CLIENT_ID and receive the coding presence
            second_rpc = _FakeRPC.instances[1]
            self.assertEqual(second_rpc.client_id, DEFAULT_CLIENT_ID)
            self.assertTrue(second_rpc.connected)
            self.assertEqual(len(second_rpc.updates), 1)
            self.assertEqual(second_rpc.updates[0]["details"], "Working on ipc_manager.py")

            ipc._halt.set()
            ipc._poke()
            await asyncio.wait_for(main_task, timeout=1.0)

        with patch("core.ipc_manager._HandshakeAwarePresence", _FakeRPC):
            asyncio.run(run_scenario())


class TestSettingsUICleanup(unittest.TestCase):
    """Verify the manual Client ID text input has been removed from SettingsPage."""

    def test_settings_page_has_no_manual_client_id_input(self) -> None:
        from ui.main_window import SettingsPage

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            rules_src = Path(__file__).resolve().parent.parent / "config" / "rules.json"
            rules_dst = root / "rules.json"
            rules_dst.write_text(rules_src.read_text(encoding="utf-8"), encoding="utf-8")

            settings = Settings.load(root / "settings.json")
            rules = RuleBook.load(rules_dst)
            state = StateManager(settings, rules_dst, rules)
            page = SettingsPage(state)

            self.assertFalse(
                hasattr(page, "_client_id_edit"),
                "Manual Client ID QLineEdit (_client_id_edit) must be removed from SettingsPage",
            )
            self.assertFalse(
                hasattr(page, "_use_custom_id_switch"),
                "Manual Custom Client ID toggle (_use_custom_id_switch) must be removed from SettingsPage",
            )


if __name__ == "__main__":
    unittest.main()

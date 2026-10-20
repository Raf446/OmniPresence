"""Tests for Discord profile extraction, cross-platform IPC paths, and Spotify/Genius deep-link buttons."""
from __future__ import annotations

import unittest

from core.ipc_manager import (
    IPCManager,
    build_user_profile,
    discord_ipc_candidates,
    resolve_ipc_path,
)
from core.priority_engine import PresenceData, sanitize_buttons
from extractors.media.links import (
    DISCORD_BUTTON_LABEL_LIMIT,
    DISCORD_BUTTON_URL_LIMIT,
    build_media_buttons,
    spotify_track_url_from_id,
)

UID = "80351110224678912"


class UserProfileTests(unittest.TestCase):
    def test_static_avatar_banner_and_accent_color(self) -> None:
        profile = build_user_profile(
            {
                "id": UID,
                "username": "raf",
                "global_name": "Raf",
                "discriminator": "0",
                "avatar": "abcdef",
                "banner": "bn123",
                "accent_color": 0x1ABC9C,
            }
        )
        self.assertEqual(profile["avatar_url"], f"https://cdn.discordapp.com/avatars/{UID}/abcdef.png?size=256")
        self.assertEqual(profile["banner_url"], f"https://cdn.discordapp.com/banners/{UID}/bn123.png?size=600")
        self.assertEqual(profile["accent_color"], "#1ABC9C")
        self.assertEqual(profile["color"], "#1ABC9C")
        self.assertEqual((profile["display_name"], profile["handle"]), ("Raf", "@raf"))

    def test_animated_avatar_uses_gif(self) -> None:
        profile = build_user_profile({"id": UID, "username": "raf", "avatar": "a_deadbeef"})
        self.assertEqual(profile["avatar_url"], f"https://cdn.discordapp.com/avatars/{UID}/a_deadbeef.gif")

    def test_default_avatar_and_no_banner_fallback(self) -> None:
        profile = build_user_profile({"id": UID, "username": "raf", "avatar": None, "banner_color": "#ff8800"})
        expected = (int(UID) >> 22) % 6
        self.assertEqual(profile["avatar_url"], f"https://cdn.discordapp.com/embed/avatars/{expected}.png")
        self.assertEqual(profile["banner_url"], "")
        self.assertEqual(profile["color"], "#FF8800")

    def test_ipc_manager_exposes_profile_signal(self) -> None:
        self.assertTrue(hasattr(IPCManager, "user_profile_updated"))


class IpcPathTests(unittest.TestCase):
    def test_linux_prefers_xdg_runtime_dir(self) -> None:
        paths = discord_ipc_candidates(0, platform="linux", env={"XDG_RUNTIME_DIR": "/run/user/1000"})
        self.assertEqual(paths[0], "/run/user/1000/discord-ipc-0")
        self.assertIn("/run/user/1000/snap.discord/discord-ipc-0", paths)
        self.assertEqual(paths[-1].rsplit("/", 1)[-1], "discord-ipc-0")

    def test_windows_uses_named_pipe(self) -> None:
        self.assertEqual(discord_ipc_candidates(0, platform="win32")[0], r"\\.\pipe\discord-ipc-0")

    def test_resolve_picks_first_existing_unix_socket(self) -> None:
        live = "/run/user/1000/discord-ipc-1"
        found = resolve_ipc_path(
            None, platform="linux", env={"XDG_RUNTIME_DIR": "/run/user/1000"}, exists=lambda p: p == live
        )
        self.assertEqual(found, live)


class MediaButtonTests(unittest.TestCase):
    def test_search_fallback_and_genius(self) -> None:
        spotify, genius = build_media_buttons("Sialan", "Juicy Luicy")
        self.assertEqual(spotify, ("Listen on Spotify", "https://open.spotify.com/search/Sialan%20Juicy%20Luicy"))
        self.assertEqual(genius, ("Lyrics on Genius", "https://genius.com/search?q=Sialan%20Juicy%20Luicy"))

    def test_specific_spotify_track_when_id_known(self) -> None:
        url = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
        self.assertEqual(build_media_buttons("A", "B", url)[0][1], url)
        self.assertEqual(spotify_track_url_from_id("/com/spotify/track/4uLU6hMCjMI75M1A2tKUQC"), url)

    def test_limits_hold_for_huge_titles_and_survive_sanitizer(self) -> None:
        buttons = build_media_buttons("T" * 900 + "/?&", "Artist")
        self.assertEqual(len(buttons), 2)
        for label, url in buttons:
            self.assertLessEqual(len(label), DISCORD_BUTTON_LABEL_LIMIT)
            self.assertLessEqual(len(url), DISCORD_BUTTON_URL_LIMIT)
        self.assertEqual(len(sanitize_buttons(buttons)), 2)

    def test_fallback_chain_drops_buttons_then_external_art(self) -> None:
        presence = PresenceData(
            "Sialan", "by Juicy Luicy", "https://i.scdn.co/image/x", None, None, None, None, None,
            (("Listen on Spotify", "https://open.spotify.com/search/x"),), 2, tier=5, rule_id="spotify",
        )
        chain = IPCManager._fallback_chain(presence)
        self.assertEqual([stage for stage, _ in chain], ["without buttons", "with fallback icon (spotify)"])
        self.assertEqual(chain[0][1].buttons, ())
        self.assertEqual(chain[1][1].large_image, "spotify")


if __name__ == "__main__":
    unittest.main()

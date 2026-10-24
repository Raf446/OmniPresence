"""Discord IPC lifecycle: connect, capture READY user profile, reconnect with backoff, coalesce and rate-limit updates."""
from __future__ import annotations

import asyncio
import json
import os
import random
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import replace
from enum import Enum
from typing import Any

from PySide6.QtCore import QThread, Signal
from pypresence import (
    ActivityType,
    AioPresence,
    DiscordError,
    DiscordNotFound,
    InvalidID,
    InvalidPipe,
    PyPresenceException,
    ServerError,
)
from pypresence.utils import get_ipc_path

from core.priority_engine import PresenceData
from core.settings_manager import (
    DEFAULT_CLIENT_ID,
    DYNAMIC_CLIENT_IDS,
    SPOTIFY_CLIENT_ID,
    VLC_CLIENT_ID,
    YOUTUBE_CLIENT_ID,
    resolve_client_id,
)


class ConnectionState(str, Enum):
    CONNECTED = "connected"
    CONNECTING = "connecting"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class Backoff:
    def __init__(self, base: float = 2.0, cap: float = 60.0) -> None:
        self._base = base
        self._cap = cap
        self._attempt = 0

    def reset(self) -> None:
        self._attempt = 0

    def next_delay(self) -> float:
        delay = min(self._cap, self._base * (2**self._attempt))
        self._attempt += 1
        return delay * random.uniform(0.8, 1.2)


class _ConnectionLost(Exception):
    """The pipe broke or Discord stopped answering."""


DISCORD_CDN = "https://cdn.discordapp.com"
_LINUX_IPC_SUBDIRS = ("", "app/com.discordapp.Discord/", "snap.discord/", "app/com.discordapp.DiscordCanary/")


def discord_ipc_candidates(
    pipe: int | None = None,
    platform: str | None = None,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Ordered Discord IPC endpoints: Windows named pipes, or Unix domain sockets under $XDG_RUNTIME_DIR."""
    plat = platform or sys.platform
    environ = os.environ if env is None else env
    indices = [pipe] if pipe is not None else list(range(10))
    if plat.startswith("win"):
        return [rf"\\.\pipe\discord-ipc-{i}" for i in indices]

    bases: list[str] = []
    for var in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP"):
        value = (environ.get(var) or "").strip().rstrip("/")
        if value and value not in bases:
            bases.append(value)
    if "/tmp" not in bases:
        bases.append("/tmp")
    return [f"{base}/{sub}discord-ipc-{i}" for base in bases for sub in _LINUX_IPC_SUBDIRS for i in indices]


def resolve_ipc_path(
    pipe: int | None = None,
    platform: str | None = None,
    env: dict[str, str] | None = None,
    exists=os.path.exists,
) -> str | None:
    """Locate the live Discord IPC endpoint for the current OS (None when Discord is not running)."""
    plat = platform or sys.platform
    if not plat.startswith("win"):
        for candidate in discord_ipc_candidates(pipe, plat, env):
            if exists(candidate):
                return candidate
    return get_ipc_path(pipe)


def _default_avatar_index(user_id: str) -> int:
    try:
        return (int(user_id) >> 22) % 6
    except (TypeError, ValueError):
        return 0


def _hex_color(value: Any) -> str:
    """Normalize Discord's int RGB or '#RRGGBB' / 'RRGGBB' strings to '#RRGGBB' ('' when invalid)."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return f"#{value & 0xFFFFFF:06X}" if value >= 0 else ""
    text = str(value).strip().lstrip("#")
    if len(text) == 6:
        try:
            int(text, 16)
        except ValueError:
            return ""
        return f"#{text.upper()}"
    return ""


def extract_user_identity(user: Any) -> tuple[str, str]:
    """Extract (display_name, handle) from Discord's READY user dictionary."""
    if not isinstance(user, dict):
        return "", ""
    username = str(user.get("username") or "").strip()
    global_name = str(user.get("global_name") or "").strip()
    discriminator = str(user.get("discriminator") or "").strip()

    display_name = global_name or username
    if username:
        if discriminator and discriminator != "0":
            handle = f"@{username}#{discriminator}"
        else:
            handle = f"@{username}"
    elif global_name:
        handle = f"@{global_name.lower().replace(' ', '_')}"
    else:
        handle = ""
    return display_name, handle


def build_user_profile(user: Any) -> dict[str, str]:
    """Build the full profile dict (identity, CDN avatar/banner URLs, banner fallback color) from READY."""
    if not isinstance(user, dict):
        return {}
    user_id = str(user.get("id") or "").strip()
    avatar = str(user.get("avatar") or "").strip()
    banner = str(user.get("banner") or "").strip()

    if avatar and user_id:
        if avatar.startswith("a_"):
            avatar_url = f"{DISCORD_CDN}/avatars/{user_id}/{avatar}.gif"
        else:
            avatar_url = f"{DISCORD_CDN}/avatars/{user_id}/{avatar}.png?size=256"
    else:
        avatar_url = f"{DISCORD_CDN}/embed/avatars/{_default_avatar_index(user_id)}.png"
    banner_url = f"{DISCORD_CDN}/banners/{user_id}/{banner}.png?size=600" if (banner and user_id) else ""

    accent = _hex_color(user.get("accent_color"))
    banner_color = _hex_color(user.get("banner_color"))
    display_name, handle = extract_user_identity(user)
    return {
        "id": user_id,
        "username": str(user.get("username") or "").strip(),
        "global_name": str(user.get("global_name") or "").strip(),
        "discriminator": str(user.get("discriminator") or "").strip(),
        "display_name": display_name,
        "handle": handle,
        "avatar": avatar,
        "avatar_url": avatar_url,
        "banner": banner,
        "banner_url": banner_url,
        "accent_color": accent,
        "banner_color": banner_color,
        "color": banner_color or accent,
    }


class _HandshakeAwarePresence(AioPresence):
    """Subclasses AioPresence to capture the READY payload (including active Discord user info) during IPC handshake."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ready_user: dict[str, Any] = {}

    async def handshake(self) -> None:
        ipc_path = resolve_ipc_path(self.pipe)
        if not ipc_path:
            raise DiscordNotFound

        await self.create_reader_writer(ipc_path)
        self.send_data(0, {"v": 1, "client_id": self.client_id})
        assert self.sock_reader is not None
        preamble = await self.sock_reader.read(8)
        if len(preamble) < 8:
            raise InvalidPipe
        _, length = struct.unpack("<ii", preamble)
        raw_payload = await self.sock_reader.read(length)
        data = json.loads(raw_payload.decode("utf-8", errors="replace"))
        if "code" in data:
            if data.get("message") == "Invalid Client ID":
                raise InvalidID
            raise DiscordError(data["code"], data.get("message", "Discord IPC error"))

        # Capture user object from READY dispatch (supports both data['data']['user'] and data['user'])
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        user = inner.get("user") or data.get("user")
        if isinstance(user, dict):
            self.ready_user = user

        if self._events_on:
            self.sock_reader.feed_data = self.on_event


class IPCManager(QThread):
    status_changed = Signal(str, str)
    message = Signal(str, str)
    presence_sent = Signal(object)
    user_detected = Signal(str, str)
    user_profile_updated = Signal(dict)

    # Discord allows up to 5 RPC updates per 20s window (1 update per 5.0s sustained).
    # Aligned with the 5.0s (5000ms) MediaScrobbler polling rhythm so updates flow without queue buildup.
    WINDOW_SECONDS = 20.0
    WINDOW_LIMIT = 4
    SUSTAINED_GAP_SECONDS = 5.0
    PRIORITY_SWITCH_GAP = 0.25
    HEARTBEAT_INTERVAL = 40.0
    HEARTBEAT_SECONDS = 40.0
    LIVENESS_CHECK_SECONDS = 5.0
    CONNECT_TIMEOUT = 15.0
    REHANDSHAKE_DEBOUNCE_SECONDS = 0.35

    def __init__(self, client_id: str = DEFAULT_CLIENT_ID, parent=None) -> None:
        super().__init__(parent)
        self._client_id = (client_id or DEFAULT_CLIENT_ID).strip()
        self._default_client_id = self._client_id
        self._lock = threading.Lock()
        self._desired: PresenceData | None = None
        self._last_pushed: PresenceData | None = None
        self._last_pushed_payload: tuple | None = None
        self.pending_activity: PresenceData | None = None
        self._has_pending: bool = False
        self._pending_staged_at: float = 0.0
        self._cooldown_active: bool = False
        self._cooldown_until: float = 0.0
        self._halt = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._status = ("", "")

    @property
    def client_id(self) -> str:
        with self._lock:
            return self._client_id

    @staticmethod
    def resolve_client_id_for_presence(
        presence: PresenceData | None,
        default_id: str = DEFAULT_CLIENT_ID,
        custom_app_id: str | None = None,
    ) -> str:
        """Return the target Discord Application ID for `presence` (or `default_id` when idle/None)."""
        rule_id = presence.rule_id if presence is not None else None
        return resolve_client_id(rule_id, default_id=default_id, custom_app_id=custom_app_id)

    @staticmethod
    def _is_paused_presence(presence: PresenceData | None) -> bool:
        """Return True if `presence` represents a paused media state."""
        if presence is None:
            return False
        return (
            presence.large_image == "pause"
            or presence.small_image == "pause"
            or (presence.details is not None and presence.details.startswith("[Paused]"))
            or (presence.start is None and presence.end is None and presence.tier == 5)
        )

    @classmethod
    def _payload_signature(cls, presence: PresenceData | None) -> tuple | None:
        """Canonical deduplication signature ignoring linear clock drift."""
        if presence is None:
            return None
        state_flag = "paused" if cls._is_paused_presence(presence) else "playing"
        return (
            presence.rule_id,
            presence.tier,
            presence.activity_type,
            presence.details,
            presence.state,
            presence.large_image,
            presence.large_text,
            presence.small_image,
            presence.small_text,
            presence.buttons,
            state_flag,
        )

    @staticmethod
    def _is_play_pause_transition(prev: PresenceData | None, curr: PresenceData | None) -> bool:
        """Return True when transitioning between Playing and Paused on the same active media rule."""
        if prev is None or curr is None:
            return False
        if prev.rule_id != curr.rule_id:
            return False
        return (
            (prev.large_image == "pause") != (curr.large_image == "pause")
            or (prev.small_image == "pause") != (curr.small_image == "pause")
            or (prev.start is None) != (curr.start is None)
        )

    @staticmethod
    def _is_priority_transition(prev: PresenceData | None, curr: PresenceData | None) -> bool:
        """Return True only when one of the 4 valid event-driven IPC emission criteria is met:
        (a) Playback status transition (playing <-> paused <-> stopped),
        (b) Manual seek (abs(curr_start - prev_start) > 2.0s),
        (c) Media / episode / poster change (details, state, large_image, large_text, small_image, buttons),
        (d) Context / application / rule switch (rule_id, tier, activity_type).
        """
        if (prev is None) != (curr is None):
            return True
        if prev is None or curr is None:
            return False
        start_shifted = (
            (prev.start is None) != (curr.start is None)
            or (prev.end is None) != (curr.end is None)
            or (prev.start is not None and curr.start is not None and abs(curr.start - prev.start) > 2.0)
            or (prev.end is not None and curr.end is not None and abs(curr.end - prev.end) > 2.0)
        )
        return (
            curr.tier == 1
            or curr.tier != prev.tier
            or curr.rule_id != prev.rule_id
            or curr.activity_type != prev.activity_type
            or curr.details != prev.details
            or curr.state != prev.state
            or curr.large_image != prev.large_image
            or curr.large_text != prev.large_text
            or curr.small_image != prev.small_image
            or curr.small_text != prev.small_text
            or curr.buttons != prev.buttons
            or start_shifted
        )

    @staticmethod
    def extract_user_identity(user: MappingOrDict) -> tuple[str, str]:
        """Extract (display_name, handle) from Discord's READY user dictionary."""
        return extract_user_identity(user)

    @staticmethod
    def build_user_profile(user: MappingOrDict) -> dict[str, str]:
        """Full profile (identity + CDN avatar/banner + banner color) from Discord's READY user object."""
        return build_user_profile(user)

    def submit(self, presence: PresenceData | None) -> None:
        """Store the latest presence in pending_activity buffer, auto-switch Client ID if needed, and wake the IPC worker."""
        with self._lock:
            now = time.monotonic()
            if presence is not None and presence.rule_id and presence.rule_id != "manual-override":
                target_id = self.resolve_client_id_for_presence(presence, default_id=self._default_client_id)
                if target_id != self._client_id:
                    self._client_id = target_id
                    self._last_pushed = None
                    self._last_pushed_payload = None
                    self._cooldown_active = False
                    self._cooldown_until = 0.0

            in_cooldown = self._cooldown_active or (now < self._cooldown_until)
            if (
                in_cooldown
                and self._has_pending
                and self.pending_activity is not None
                and self.pending_activity.tier == 5
                and presence is not None
                and presence.tier >= 6
            ):
                return

            incoming_sig = self._payload_signature(presence)
            if (
                self._last_pushed_payload is not None
                and incoming_sig == self._last_pushed_payload
                and not self._is_priority_transition(self._last_pushed, presence)
                and not in_cooldown
            ):
                # Strict deduplication: identical paused or playing payload without seek (> 2.0s) is cancelled
                self._desired = self._last_pushed
                self.pending_activity = self._last_pushed
                self._has_pending = False
                self._pending_staged_at = 0.0
                return

            self._desired = presence
            self.pending_activity = presence
            if presence == self._last_pushed and self._last_pushed_payload is not None and not in_cooldown:
                self._has_pending = False
                self._pending_staged_at = 0.0
                return
            if self._is_play_pause_transition(self._last_pushed, presence) or self._is_priority_transition(
                self._last_pushed, presence
            ):
                self._cooldown_active = False
                self._cooldown_until = 0.0
            self._has_pending = True
            self._pending_staged_at = now
        self._poke()

    def set_client_id(self, client_id: str) -> None:
        cleaned = (client_id or "").strip()
        if not cleaned:
            return
        with self._lock:
            if cleaned not in DYNAMIC_CLIENT_IDS.values():
                self._default_client_id = cleaned
            if cleaned == self._client_id:
                return
            self._client_id = cleaned
            # Force re-dispatch of the active presence once the new Client ID socket handshakes
            self._last_pushed = None
            self._last_pushed_payload = None
            if self._desired is not None:
                self.pending_activity = self._desired
                self._has_pending = True
                self._pending_staged_at = time.monotonic()
            self._cooldown_active = False
            self._cooldown_until = 0.0
        self._poke()

    def stop(self) -> None:
        self._halt.set()
        self._poke()
        self.wait(5000)

    def _poke(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None and not loop.is_closed():
            loop.call_soon_threadsafe(wake.set)

    def _snapshot(self) -> tuple[str, PresenceData | None, bool]:
        with self._lock:
            active_payload = self.pending_activity if self._has_pending else self._desired
            return self._client_id, active_payload, self._has_pending

    def _stage_pending(self, presence: PresenceData | None) -> None:
        """Buffer an activity update when blocked by Discord IPC rate-limit cooldown.

        Priority Guard: Protect pending P5 (Media Playing) payload from being overwritten
        by a transient P6 (Standby CPU/Temp) payload while cooldown is active.
        """
        with self._lock:
            if (
                self._has_pending
                and self.pending_activity is not None
                and self.pending_activity.tier == 5
                and presence is not None
                and presence.tier >= 6
            ):
                return
            self._desired = presence
            self.pending_activity = presence
            self._has_pending = True
            if self._pending_staged_at <= 0.0:
                self._pending_staged_at = time.monotonic()

    def _consume_pending(self) -> PresenceData | None:
        """Atomically pop the latest buffered pending_activity ready for IPC transmission."""
        with self._lock:
            payload = self.pending_activity if self._has_pending else self._desired
            self._desired = payload
            self._last_pushed = payload
            self._last_pushed_payload = self._payload_signature(payload)
            self._has_pending = False
            self._pending_staged_at = 0.0
            self._cooldown_active = False
            self._cooldown_until = 0.0
            return payload

    def _set_status(self, state: ConnectionState, detail: str = "") -> None:
        if self._status != (state.value, detail):
            self._status = (state.value, detail)
            self.status_changed.emit(state.value, detail)

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception as exc:
            self.message.emit("error", f"IPC thread crashed: {exc!r}")
            self._set_status(ConnectionState.ERROR, "IPC thread crashed")
        finally:
            self._loop = None
            loop.close()

    async def _sleep(self, seconds: float | None) -> None:
        assert self._wake is not None
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _main(self) -> None:
        self._wake = asyncio.Event()
        backoff = Backoff()
        while not self._halt.is_set():
            self._wake.clear()
            client_id, _, _ = self._snapshot()
            if not client_id.isdigit():
                self._set_status(ConnectionState.ERROR, "Discord Application ID is not set")
                await self._sleep(None)
                continue

            self._set_status(ConnectionState.CONNECTING, "Connecting to Discord")
            rpc = _HandshakeAwarePresence(client_id, loop=asyncio.get_running_loop())
            try:
                await asyncio.wait_for(rpc.connect(), timeout=self.CONNECT_TIMEOUT)
            except InvalidID:
                self._set_status(ConnectionState.ERROR, "Discord rejected the Application ID")
                self.message.emit("error", "Discord rejected the Application ID, check discord_client_id")
                self._close(rpc)
                await self._sleep(None)
                continue
            except (DiscordNotFound, PyPresenceException, OSError, asyncio.TimeoutError) as exc:
                delay = backoff.next_delay()
                reason = "Discord is not running" if isinstance(exc, DiscordNotFound) else type(exc).__name__
                self._set_status(ConnectionState.DISCONNECTED, f"{reason}, retrying in {delay:.0f}s")
                self._close(rpc)
                await self._sleep(delay)
                continue

            backoff.reset()
            display_name, handle = self.extract_user_identity(rpc.ready_user)
            profile = self.build_user_profile(rpc.ready_user)
            if profile:
                self.user_profile_updated.emit(profile)
            if display_name or handle:
                self.user_detected.emit(display_name, handle)
                self._set_status(ConnectionState.CONNECTED, f"Connected ({handle or display_name})")
                self.message.emit("info", f"Discord IPC connected as {display_name} ({handle})".strip())
            else:
                self._set_status(ConnectionState.CONNECTED, "Connected to Discord")
                self.message.emit("info", "Discord IPC connected")

            try:
                await self._serve(rpc, client_id)
            except _ConnectionLost as exc:
                delay = backoff.next_delay()
                self.message.emit("warn", f"Discord connection lost ({exc}), reconnecting")
                self._set_status(ConnectionState.DISCONNECTED, f"Connection lost, retrying in {delay:.0f}s")
                self._close(rpc)
                await self._sleep(delay)
                continue
            await self._farewell(rpc)
            if not self._halt.is_set():
                # Brief debounce before re-handshaking with the newly selected Client ID
                await asyncio.sleep(self.REHANDSHAKE_DEBOUNCE_SECONDS)

    async def _serve(self, rpc: AioPresence, client_id: str) -> None:
        sent_at: deque[float] = deque()
        last_pushed: PresenceData | None = None
        pushed_once = False
        last_io = time.monotonic()

        while not self._halt.is_set():
            self._wake.clear()
            current_id, desired, has_pending = self._snapshot()
            if current_id != client_id:
                return
            reader = getattr(rpc, "sock_reader", None)
            if reader is not None and reader.at_eof():
                raise _ConnectionLost("pipe closed by Discord")

            now = time.monotonic()
            # Evict timestamps older than the 20s window (with 0.2s tolerance for 5.0s QTimer tick alignment)
            while sent_at and now - sent_at[0] >= (self.WINDOW_SECONDS - 0.20):
                sent_at.popleft()

            is_playing_active = (
                pushed_once
                and last_pushed is not None
                and not self._is_paused_presence(last_pushed)
                and last_pushed.start is not None
            )
            heartbeat_due = is_playing_active and (now - last_io >= self.HEARTBEAT_INTERVAL)
            transition_due = (not pushed_once) or self._is_priority_transition(last_pushed, desired)

            due = transition_due or has_pending or heartbeat_due
            if due:
                # Strict deduplication: if already pushed, no transition occurred, and 40s playing heartbeat is not due, cancel send
                if pushed_once and not transition_due and not heartbeat_due:
                    self._consume_pending()
                    if is_playing_active:
                        rem = max(self.HEARTBEAT_INTERVAL - (now - last_io), 0.05)
                        await self._sleep(min(rem, self.LIVENESS_CHECK_SECONDS))
                    else:
                        await self._sleep(self.LIVENESS_CHECK_SECONDS)
                    continue

                is_immediate = (
                    (not pushed_once)
                    or self._is_play_pause_transition(last_pushed, desired)
                    or transition_due
                )
                wait = 0.0
                if sent_at and not is_immediate and not heartbeat_due:
                    wait = max(wait, self.SUSTAINED_GAP_SECONDS - (now - sent_at[-1]))
                    if len(sent_at) >= self.WINDOW_LIMIT:
                        wait = max(wait, (self.WINDOW_SECONDS - 0.15) - (now - sent_at[0]))

                if wait <= 0:
                    payload_to_send = self._consume_pending()
                    await self._push(rpc, payload_to_send)
                    last_pushed, pushed_once = payload_to_send, True
                    last_io = time.monotonic()
                    sent_at.append(last_io)
                    if transition_due:
                        self.presence_sent.emit(payload_to_send)
                    continue

                # Rate-limit cooldown active: keep latest payload in pending_activity buffer and wake as soon as cooldown expires
                with self._lock:
                    self._cooldown_active = True
                    self._cooldown_until = now + wait
                self._stage_pending(desired)
                timeout = wait
            else:
                if is_playing_active:
                    timeout = max(self.HEARTBEAT_INTERVAL - (now - last_io), 0.05)
                else:
                    timeout = self.LIVENESS_CHECK_SECONDS
            await self._sleep(min(max(timeout, 0.05), self.LIVENESS_CHECK_SECONDS))

    @staticmethod
    def _fallback_chain(presence: PresenceData) -> list[tuple[str, PresenceData]]:
        """Progressively simpler variants: drop buttons first, then swap external artwork for a bundled icon."""
        chain: list[tuple[str, PresenceData]] = []
        current = presence
        if current.buttons:
            current = replace(current, buttons=())
            chain.append(("without buttons", current))
        if current.large_image and current.large_image.lower().startswith(("http://", "https://")):
            if "youtube" in current.rule_id:
                icon = "youtube"
            elif "github" in current.rule_id:
                icon = "github"
            elif "spotify" in current.rule_id:
                icon = "spotify"
            elif "vlc" in current.rule_id:
                icon = "vlc"
            else:
                icon = "music_placeholder"
            current = replace(current, large_image=icon)
            chain.append((f"with fallback icon ({icon})", current))
        return chain

    async def _push(self, rpc: AioPresence, presence: PresenceData | None) -> None:
        try:
            if presence is None:
                await rpc.clear()
                return
            await self._update(rpc, presence)
        except ServerError as exc:
            if presence is not None:
                for stage, fallback in self._fallback_chain(presence):
                    self.message.emit("warn", f"Discord rejected the presence ({exc}), retrying {stage}")
                    try:
                        await self._update(rpc, fallback)
                        return
                    except ServerError as retry_exc:
                        exc = retry_exc
                    except (PyPresenceException, OSError, asyncio.TimeoutError, AssertionError) as lost:
                        raise _ConnectionLost(type(lost).__name__) from lost
            self.message.emit("error", f"Discord rejected the presence: {exc}")
        except (PyPresenceException, OSError, asyncio.TimeoutError, AssertionError) as exc:
            raise _ConnectionLost(type(exc).__name__) from exc

    @staticmethod
    async def _update(rpc: AioPresence, presence: PresenceData) -> None:
        await rpc.update(activity_type=ActivityType(presence.activity_type), **presence.to_kwargs())

    async def _farewell(self, rpc: AioPresence) -> None:
        try:
            await asyncio.wait_for(rpc.clear(), timeout=3)
        except Exception:
            pass
        self._close(rpc)
        if not self._halt.is_set():
            return
        self._set_status(ConnectionState.DISCONNECTED, "Stopped")

    @staticmethod
    def _close(rpc: AioPresence) -> None:
        # pypresence's own close() also closes the event loop, which this thread keeps using.
        writer = getattr(rpc, "sock_writer", None)
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass


MappingOrDict = dict[str, Any]

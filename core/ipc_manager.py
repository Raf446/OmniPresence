"""Discord IPC lifecycle: connect, capture READY user profile, reconnect with backoff, coalesce and rate-limit updates."""
from __future__ import annotations

import asyncio
import json
import random
import struct
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


class _HandshakeAwarePresence(AioPresence):
    """Subclasses AioPresence to capture the READY payload (including active Discord user info) during IPC handshake."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ready_user: dict[str, Any] = {}

    async def handshake(self) -> None:
        ipc_path = get_ipc_path(self.pipe)
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

    # Discord allows up to 5 RPC updates per 20s window (~4.5s sustained).
    # We reserve burst tokens for priority/context switches (0.25s gap) while spacing sustained updates at 4.5s.
    WINDOW_SECONDS = 20.0
    WINDOW_LIMIT = 4
    SUSTAINED_GAP_SECONDS = 4.5
    PRIORITY_SWITCH_GAP = 0.25
    HEARTBEAT_SECONDS = 15.0
    LIVENESS_CHECK_SECONDS = 5.0
    CONNECT_TIMEOUT = 15.0

    def __init__(self, client_id: str, parent=None) -> None:
        super().__init__(parent)
        self._client_id = client_id.strip()
        self._lock = threading.Lock()
        self._desired: PresenceData | None = None
        self._halt = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._status = ("", "")

    @staticmethod
    def _is_priority_transition(prev: PresenceData | None, curr: PresenceData | None) -> bool:
        """Return True when switching rules/tiers, changing track/details/buttons, or upgrading placeholder to artwork."""
        if (prev is None) != (curr is None):
            return True
        if prev is None or curr is None:
            return False
        return (
            curr.tier == 1
            or curr.tier != prev.tier
            or curr.rule_id != prev.rule_id
            or curr.details != prev.details
            or curr.state != prev.state
            or curr.large_image != prev.large_image
            or curr.small_image != prev.small_image
            or curr.buttons != prev.buttons
        )

    @staticmethod
    def extract_user_identity(user: MappingOrDict) -> tuple[str, str]:
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

    def submit(self, presence: PresenceData | None) -> None:
        """Replace any queued pending payload with the latest state and wake the IPC worker immediately."""
        with self._lock:
            self._desired = presence
        self._poke()

    def set_client_id(self, client_id: str) -> None:
        with self._lock:
            self._client_id = client_id.strip()
        self._poke()

    def stop(self) -> None:
        self._halt.set()
        self._poke()
        self.wait(5000)

    def _poke(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None and not loop.is_closed():
            loop.call_soon_threadsafe(wake.set)

    def _snapshot(self) -> tuple[str, PresenceData | None]:
        with self._lock:
            return self._client_id, self._desired

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
            client_id, _ = self._snapshot()
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

    async def _serve(self, rpc: AioPresence, client_id: str) -> None:
        sent_at: deque[float] = deque()
        last_pushed: PresenceData | None = None
        pushed_once = False
        last_io = time.monotonic()

        while not self._halt.is_set():
            self._wake.clear()
            current_id, desired = self._snapshot()
            if current_id != client_id:
                return
            reader = getattr(rpc, "sock_reader", None)
            if reader is not None and reader.at_eof():
                raise _ConnectionLost("pipe closed by Discord")

            now = time.monotonic()
            while sent_at and now - sent_at[0] > self.WINDOW_SECONDS:
                sent_at.popleft()
            due = (not pushed_once) or desired != last_pushed or now - last_io >= self.HEARTBEAT_SECONDS
            if due:
                wait = 0.0
                if sent_at:
                    is_switch = (not pushed_once) or self._is_priority_transition(last_pushed, desired)
                    min_gap = (
                        self.PRIORITY_SWITCH_GAP
                        if (is_switch and len(sent_at) < self.WINDOW_LIMIT - 1)
                        else self.SUSTAINED_GAP_SECONDS
                    )
                    wait = max(wait, min_gap - (now - sent_at[-1]))
                if len(sent_at) >= self.WINDOW_LIMIT:
                    wait = max(wait, self.WINDOW_SECONDS - (now - sent_at[0]) + 0.1)
                if wait <= 0:
                    await self._push(rpc, desired)
                    last_pushed, pushed_once = desired, True
                    last_io = time.monotonic()
                    sent_at.append(last_io)
                    self.presence_sent.emit(desired)
                    continue
                timeout = wait
            else:
                timeout = self.HEARTBEAT_SECONDS - (now - last_io)
            await self._sleep(min(timeout, self.LIVENESS_CHECK_SECONDS))

    async def _push(self, rpc: AioPresence, presence: PresenceData | None) -> None:
        try:
            if presence is None:
                await rpc.clear()
                return
            await self._update(rpc, presence)
        except ServerError as exc:
            if presence is not None:
                fallback = presence
                if fallback.buttons:
                    self.message.emit("warn", f"Discord rejected the presence ({exc}), retrying without buttons")
                    fallback = replace(fallback, buttons=())
                elif fallback.large_image and fallback.large_image.lower().startswith(("http://", "https://")):
                    self.message.emit("warn", f"Discord rejected external artwork URL ({exc}), using fallback icon")
                    if "youtube" in fallback.rule_id:
                        fallback_icon = "youtube"
                    elif "github" in fallback.rule_id:
                        fallback_icon = "github"
                    elif "spotify" in fallback.rule_id:
                        fallback_icon = "spotify"
                    else:
                        fallback_icon = "music_placeholder"
                    fallback = replace(fallback, large_image=fallback_icon)
                if fallback != presence:
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

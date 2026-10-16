"""Discord IPC lifecycle: connect, reconnect with backoff, coalesce and rate-limit updates."""
from __future__ import annotations

import asyncio
import random
import threading
import time
from collections import deque
from dataclasses import replace
from enum import Enum

from PySide6.QtCore import QThread, Signal
from pypresence import ActivityType, AioPresence, DiscordNotFound, InvalidID, PyPresenceException, ServerError

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


class IPCManager(QThread):
    status_changed = Signal(str, str)
    message = Signal(str, str)
    presence_sent = Signal(object)

    # Discord documents five activity updates per twenty seconds; stay one below.
    WINDOW_SECONDS = 20.0
    WINDOW_LIMIT = 4
    MIN_GAP_SECONDS = 1.0
    HEARTBEAT_SECONDS = 45.0
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

    def submit(self, presence: PresenceData | None) -> None:
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
            rpc = AioPresence(client_id, loop=asyncio.get_running_loop())
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
                    wait = max(wait, self.MIN_GAP_SECONDS - (now - sent_at[-1]))
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
            if presence is not None and presence.buttons:
                self.message.emit("warn", f"Discord rejected the presence ({exc}), retrying without buttons")
                try:
                    await self._update(rpc, replace(presence, buttons=()))
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

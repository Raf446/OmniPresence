"""Windows System Media Transport Controls listener (Spotify, Apple Music, browsers, VLC...)."""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from PySide6.QtCore import QThread, Signal

try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )

    SMTC_AVAILABLE = True
except Exception:  # winsdk missing, or not Windows
    SessionManager = None
    PlaybackStatus = None
    SMTC_AVAILABLE = False

POLL_FALLBACK_SECONDS = 1.5
SEEK_TOLERANCE_SECONDS = 3.0


@dataclass(frozen=True)
class MediaInfo:
    app_id: str
    title: str
    artist: str
    album: str
    duration: float
    start_at: float | None
    end_at: float | None

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.app_id, self.title, self.artist, self.album)


class MediaScrobbler(QThread):
    """Runs an asyncio loop that watches SMTC sessions and emits the active track."""

    media_changed = Signal(object)
    message = Signal(str, str)

    def __init__(self, preferred_apps: list[str] | None = None, parent=None) -> None:
        super().__init__(parent)
        self._preferred = [app.lower() for app in (preferred_apps or [])]
        self._halt = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._hooked: set[str] = set()
        self._handlers: list = []
        self._last_identity: tuple | None = None
        self._last_start: float | None = None

    def stop(self) -> None:
        self._halt.set()
        self._poke()
        self.wait(3000)

    def _poke(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None and not loop.is_closed():
            loop.call_soon_threadsafe(wake.set)

    def run(self) -> None:
        if not SMTC_AVAILABLE:
            self.message.emit("warn", "winsdk is unavailable: media detection is disabled")
            return
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._watch())
        except Exception as exc:
            self.message.emit("error", f"media listener stopped: {exc!r}")
        finally:
            self._loop = None
            loop.close()

    async def _watch(self) -> None:
        self._wake = asyncio.Event()
        manager = await SessionManager.request_async()
        self._subscribe_manager(manager)
        self.message.emit("info", "listening for Windows media sessions")
        while not self._halt.is_set():
            self._wake.clear()
            try:
                info = await self._snapshot(manager)
            except Exception as exc:
                self.message.emit("warn", f"media poll failed: {exc!r}")
                info = None
            self._publish(info)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=POLL_FALLBACK_SECONDS)
            except asyncio.TimeoutError:
                pass
        self.media_changed.emit(None)

    def _subscribe_manager(self, manager) -> None:
        loop, wake = self._loop, self._wake

        def on_change(*_args) -> None:
            loop.call_soon_threadsafe(wake.set)

        self._handlers.append(on_change)
        try:
            manager.add_sessions_changed(on_change)
            manager.add_current_session_changed(on_change)
        except Exception as exc:
            self.message.emit("warn", f"SMTC change events unavailable, polling only: {exc!r}")

    def _subscribe_session(self, session) -> None:
        app_id = session.source_app_user_model_id
        if app_id in self._hooked:
            return
        self._hooked.add(app_id)
        loop, wake = self._loop, self._wake

        def on_change(*_args) -> None:
            loop.call_soon_threadsafe(wake.set)

        self._handlers.append(on_change)
        try:
            session.add_playback_info_changed(on_change)
            session.add_media_properties_changed(on_change)
            session.add_timeline_properties_changed(on_change)
        except Exception:
            self._hooked.discard(app_id)

    def _pick(self, playing: list):
        for preferred in self._preferred:
            for session in playing:
                if preferred in session.source_app_user_model_id.lower():
                    return session
        return playing[0] if playing else None

    async def _snapshot(self, manager) -> MediaInfo | None:
        playing = []
        for session in manager.get_sessions():
            self._subscribe_session(session)
            playback = session.get_playback_info()
            if playback is not None and playback.playback_status == PlaybackStatus.PLAYING:
                playing.append(session)
        session = self._pick(playing)
        if session is None:
            return None

        properties = await session.try_get_media_properties_async()
        title = (properties.title or "").strip()
        if not title:
            return None
        artist = (properties.artist or properties.album_artist or "").strip()
        album = (properties.album_title or "").strip()

        duration, start_at, end_at = 0.0, None, None
        timeline = session.get_timeline_properties()
        if timeline is not None:
            span = (timeline.end_time - timeline.start_time).total_seconds()
            if span > 1:
                position = (timeline.position - timeline.start_time).total_seconds()
                drift = (datetime.now(timezone.utc) - timeline.last_updated_time).total_seconds()
                position = min(max(position + max(drift, 0.0), 0.0), span)
                now = time.time()
                duration, start_at, end_at = span, now - position, now - position + span
        return MediaInfo(session.source_app_user_model_id, title, artist, album, duration, start_at, end_at)

    def _publish(self, info: MediaInfo | None) -> None:
        if info is None:
            if self._last_identity is not None:
                self._last_identity, self._last_start = None, None
                self.media_changed.emit(None)
            return
        changed = info.identity != self._last_identity
        sought = (
            info.start_at is not None
            and self._last_start is not None
            and abs(info.start_at - self._last_start) > SEEK_TOLERANCE_SECONDS
        )
        if changed or sought:
            self._last_identity, self._last_start = info.identity, info.start_at
            self.media_changed.emit(info)

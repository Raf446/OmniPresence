"""Windows backend: System Media Transport Controls (SMTC) session polling via winsdk."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from extractors.media.base import BaseMediaExtractor, RawTrack

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


class WindowsSmtcExtractor(BaseMediaExtractor):
    PLATFORM = "Windows SMTC"

    @classmethod
    def is_available(cls) -> bool:
        return SMTC_AVAILABLE

    def _read_session(self) -> RawTrack | None:
        return asyncio.run(self._read_async())

    def _pick(self, playing: list):
        for preferred in self._preferred:
            for session in playing:
                if preferred in session.source_app_user_model_id.lower():
                    return session
        return playing[0] if playing else None

    async def _read_async(self) -> RawTrack | None:
        manager = await SessionManager.request_async()
        playing = []
        for session in manager.get_sessions():
            playback = session.get_playback_info()
            if playback is not None and playback.playback_status == PlaybackStatus.PLAYING:
                playing.append(session)
        session = self._pick(playing)
        if session is None:
            return None

        properties = await session.try_get_media_properties_async()
        if properties is None:
            return None
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

        return RawTrack(
            app_id=session.source_app_user_model_id,
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            start_at=start_at,
            end_at=end_at,
        )

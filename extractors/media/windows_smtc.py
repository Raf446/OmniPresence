"""Windows backend: System Media Transport Controls (SMTC) session polling via winsdk."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from extractors.media.base import BaseMediaExtractor, RawTrack
from extractors.media.vlc_http import POLL_INTERVAL_MS as VLC_POLL_INTERVAL_MS, VlcHttpClient
from extractors.media.common import (
    MEDIA_KIND_VIDEO,
    STATUS_PAUSED,
    STATUS_PLAYING,
    classify_media_kind,
    sanitize_media_title,
    split_media_extension,
)

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


@dataclass
class PauseAnchor:
    """Playhead frozen when a pause was observed, so a resume does not inherit pause time as drift."""

    key: tuple[str, str]
    position: float
    observed_at: datetime
    resumed_at: datetime | None = None


def anchored_position(
    anchor: PauseAnchor | None,
    key: tuple[str, str],
    status: str,
    base_position: float,
    last_updated: datetime,
    now: datetime,
) -> tuple[PauseAnchor | None, float]:
    """Return (new_anchor, position_seconds).

    Paused: freeze the reported position. Playing right after a pause: if SMTC has not refreshed its
    timeline since the pause was observed, `base_position + (now - last_updated)` would include the whole
    pause and overshoot (even past the end, which makes Discord drop the bar), so resume from the frozen
    position instead and keep counting from the first resumed poll.
    """
    if status != STATUS_PLAYING:
        return PauseAnchor(key, base_position, now), base_position
    if anchor is not None and anchor.key == key and last_updated <= anchor.observed_at:
        if anchor.resumed_at is None:
            anchor.resumed_at = now
        return anchor, anchor.position + (now - anchor.resumed_at).total_seconds()
    drift = max((now - last_updated).total_seconds(), 0.0)
    return None, base_position + drift


class WindowsSmtcExtractor(BaseMediaExtractor):
    """SMTC for every player, except VLC which is read from its HTTP web interface (see `vlc_http.py`).

    VLC's own SMTC session (VLC 4 only) is used purely as a fallback while the web interface is
    unreachable. Polls every 2s so VLC pause/seek/stop show up quickly.
    """

    PLATFORM = "Windows SMTC + VLC HTTP"
    POLL_INTERVAL_MS = VLC_POLL_INTERVAL_MS

    def __init__(self, *args, vlc_client: VlcHttpClient | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._anchor: PauseAnchor | None = None
        self._vlc = vlc_client or VlcHttpClient()

    @classmethod
    def is_available(cls) -> bool:
        return True

    def _anchored_position(
        self,
        key: tuple[str, str],
        status: str,
        base_position: float,
        last_updated: datetime,
        now: datetime,
    ) -> float:
        """Current playhead in seconds, immune to SMTC timelines that were not refreshed on pause/resume."""
        self._anchor, position = anchored_position(self._anchor, key, status, base_position, last_updated, now)
        return position

    def _read_session(self) -> RawTrack | None:
        raw = self._vlc.fetch()
        notice = self._vlc.pop_notice()
        if notice:
            self.message.emit("warn", notice)
        if raw is not None:
            return raw  # VLC (playing or paused) outranks every other player
        if not SMTC_AVAILABLE or SessionManager is None:
            return None
        return asyncio.run(self._read_async(skip_vlc=self._vlc.reachable))

    @staticmethod
    def _is_vlc(session) -> bool:
        return "vlc" in (session.source_app_user_model_id or "").lower()

    def _pick(self, playing: list):
        for preferred in self._preferred:
            for session in playing:
                if preferred in session.source_app_user_model_id.lower():
                    return session
        return playing[0] if playing else None

    async def _read_async(self, skip_vlc: bool = False) -> RawTrack | None:
        manager = await SessionManager.request_async()
        playing = []
        vlc_paused = []
        for session in manager.get_sessions():
            if skip_vlc and self._is_vlc(session):
                continue  # the web interface answered and said stopped: ignore stale SMTC state
            playback = session.get_playback_info()
            if playback is None:
                continue
            if playback.playback_status == PlaybackStatus.PLAYING:
                playing.append(session)
            elif playback.playback_status == PlaybackStatus.PAUSED and self._is_vlc(session):
                vlc_paused.append(session)
        vlc_playing = [s for s in playing if self._is_vlc(s)]
        session = self._pick(vlc_playing) or self._pick(playing) or self._pick(vlc_paused)
        if session is None:
            return None
        status = STATUS_PLAYING if session in playing else STATUS_PAUSED

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
            base_position = (timeline.position - timeline.start_time).total_seconds()
            position = self._anchored_position(
                (session.source_app_user_model_id, title),
                status,
                base_position,
                timeline.last_updated_time,
                datetime.now(timezone.utc),
            )
            now = time.time()
            if span > 1:
                position = min(max(position, 0.0), span)
                duration, start_at, end_at = span, now - position, now - position + span
            elif self._is_vlc(session):
                start_at = now - max(position, 0.0)  # unknown length: running time only
        else:
            self._anchor = None

        kind = ""
        if self._is_vlc(session):
            kind = classify_media_kind(title, "", artist, album)
            if kind == MEDIA_KIND_VIDEO or split_media_extension(title)[1]:
                title = sanitize_media_title(title)
        if status != STATUS_PLAYING:
            start_at = end_at = None

        return RawTrack(
            app_id=session.source_app_user_model_id,
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            start_at=start_at,
            end_at=end_at,
            status=status,
            kind=kind,
        )

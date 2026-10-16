"""Live mockup of the Discord profile card plus the priority-tier strip."""
from __future__ import annotations

import getpass
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.priority_engine import PresenceData, Resolution, Tier
from ui import ACCENT, DIM, MUTED, STATUS_COLORS, TEXT, mono_font, ui_font

DC_CARD = "#111214"
DC_BOX = "#1e1f22"
DC_BANNER = "#2e3036"
DC_TILE = "#2b2d31"
DC_BADGE = "#383a40"
DC_ONLINE = "#23a55a"
DC_TEXT = "#f2f3f5"
DC_MUTED = "#b5bac1"
DC_TRACK = "#4e5058"

TYPE_VERBS = {0: "Playing", 2: "Listening to", 3: "Watching", 5: "Competing in"}


def format_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


class ImageCache:
    """Fetches externally hosted presence images so URL assets render as they do in Discord."""

    def __init__(self) -> None:
        self._manager = QNetworkAccessManager()
        self._pixmaps: dict[str, QPixmap] = {}
        self._pending: dict[str, list] = {}

    def request(self, url: str, callback) -> QPixmap | None:
        if url in self._pixmaps:
            return self._pixmaps[url]
        if url in self._pending:
            self._pending[url].append(callback)
            return None
        self._pending[url] = [callback]
        reply = self._manager.get(QNetworkRequest(QUrl(url)))
        reply.finished.connect(lambda r=reply, u=url: self._done(r, u))
        return None

    def _done(self, reply: QNetworkReply, url: str) -> None:
        pixmap = QPixmap()
        if reply.error() == QNetworkReply.NetworkError.NoError:
            pixmap.loadFromData(reply.readAll().data())
        reply.deleteLater()
        self._pixmaps[url] = pixmap
        for callback in self._pending.pop(url, []):
            callback()


_IMAGES: ImageCache | None = None


def image_cache() -> ImageCache:
    global _IMAGES
    if _IMAGES is None:
        _IMAGES = ImageCache()
    return _IMAGES


class ElidedLabel(QLabel):
    """Single-line label that truncates with an ellipsis instead of widening the card."""

    def __init__(self, parent=None, **kwargs) -> None:
        super().__init__(parent, **kwargs)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(20)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text
        self.setToolTip(text)
        self._refit()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._refit()

    def _refit(self) -> None:
        elided = QFontMetrics(self.font()).elidedText(self._full, Qt.TextElideMode.ElideRight, max(self.width(), 20))
        super().setText(elided)


class ProfileHeader(QWidget):
    BANNER = 64
    AVATAR = 72

    def __init__(self, name: str, parent=None) -> None:
        super().__init__(parent)
        self._initial = (name[:1] or "?").upper()
        self.setFixedHeight(self.BANNER + self.AVATAR // 2 + 6)
        self.setAccessibleName("Profile header preview")

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, self.width(), self.BANNER + 40), 10, 10)
        painter.setClipPath(clip)
        painter.fillRect(QRectF(0, 0, self.width(), self.BANNER), QColor(DC_BANNER))
        painter.setClipping(False)

        cx, cy = 16 + self.AVATAR / 2 + 6, self.BANNER + 2
        ring = self.AVATAR + 12
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(DC_CARD))
        painter.drawEllipse(QPointF(cx, cy), ring / 2, ring / 2)
        painter.setBrush(QColor(DC_TRACK))
        painter.drawEllipse(QPointF(cx, cy), self.AVATAR / 2, self.AVATAR / 2)
        painter.setPen(QColor(DC_TEXT))
        painter.setFont(ui_font(30, QFont.Weight.DemiBold))
        painter.drawText(QRectF(cx - 40, cy - 40, 80, 80), Qt.AlignmentFlag.AlignCenter, self._initial)

        dot = QPointF(cx + self.AVATAR / 2 - 8, cy + self.AVATAR / 2 - 8)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(DC_CARD))
        painter.drawEllipse(dot, 14, 14)
        painter.setBrush(QColor(DC_ONLINE))
        painter.drawEllipse(dot, 9, 9)


class ArtworkTile(QWidget):
    """Large image with the small badge overlapping its corner, as Discord lays them out."""

    LARGE = 60

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(68, 68)
        self._large = ""
        self._small = ""
        self.setAccessibleName("Presence artwork preview")

    def set_images(self, large: str | None, small: str | None, large_text: str | None, small_text: str | None) -> None:
        self._large, self._small = large or "", small or ""
        self.setToolTip(" | ".join(t for t in (large_text, small_text) if t))
        for key in (self._large, self._small):
            if key.lower().startswith(("http://", "https://")):
                image_cache().request(key, self.update)
        self.update()

    def _pixmap(self, key: str) -> QPixmap | None:
        if key.lower().startswith(("http://", "https://")):
            pixmap = image_cache().request(key, self.update)
            return pixmap if pixmap is not None and not pixmap.isNull() else None
        return None

    @staticmethod
    def _glyph(key: str, length: int) -> str:
        if key.lower().startswith(("http://", "https://")):
            return ""
        return key[:length].upper()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        tile = QRectF(0, 0, self.LARGE, self.LARGE)
        path = QPainterPath()
        path.addRoundedRect(tile, 8, 8)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(DC_TILE))
        painter.drawPath(path)
        large = self._pixmap(self._large)
        if large is not None:
            painter.setClipPath(path)
            scaled = large.scaled(
                self.LARGE, self.LARGE, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation
            )
            painter.drawPixmap(0, 0, scaled)
            painter.setClipping(False)
        else:
            painter.setPen(QColor(DC_MUTED))
            painter.setFont(mono_font(15, QFont.Weight.DemiBold))
            painter.drawText(tile, Qt.AlignmentFlag.AlignCenter, self._glyph(self._large, 2))

        if self._small:
            center = QPointF(self.LARGE - 6, self.LARGE - 6)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(DC_BOX))
            painter.drawEllipse(center, 14, 14)
            small = self._pixmap(self._small)
            badge = QPainterPath()
            badge.addEllipse(center, 11, 11)
            painter.setBrush(QColor(DC_BADGE))
            painter.drawPath(badge)
            if small is not None:
                painter.setClipPath(badge)
                painter.drawPixmap(
                    QRectF(center.x() - 11, center.y() - 11, 22, 22),
                    small,
                    QRectF(small.rect()),
                )
                painter.setClipping(False)
            else:
                painter.setPen(QColor(DC_MUTED))
                painter.setFont(mono_font(10, QFont.Weight.DemiBold))
                painter.drawText(QRectF(center.x() - 11, center.y() - 11, 22, 22), Qt.AlignmentFlag.AlignCenter, self._glyph(self._small, 1))


class ProgressTrack(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(4)
        self._fraction = 0.0

    def set_fraction(self, fraction: float) -> None:
        self._fraction = min(max(fraction, 0.0), 1.0)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(DC_TRACK))
        painter.drawRoundedRect(QRectF(0, 0, self.width(), 4), 2, 2)
        painter.setBrush(QColor(DC_TEXT))
        painter.drawRoundedRect(QRectF(0, 0, self.width() * self._fraction, 4), 2, 2)


class TierStrip(QWidget):
    """Six micro-badges for P1..P6: filled for the winner, brighter when a candidate is live."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(42)
        self._winner: Tier | None = None
        self._live: frozenset[Tier] = frozenset()
        self.setAccessibleName("Priority tiers")

    def set_state(self, winner: Tier | None, live: frozenset[Tier]) -> None:
        self._winner, self._live = winner, live
        names = ", ".join(f"P{int(t)} {t.label}" for t in sorted(live)) or "none"
        self.setToolTip(f"Live tiers: {names}")
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        tiers = list(Tier)
        gap = 4
        width = (self.width() - gap * (len(tiers) - 1)) / len(tiers)
        for index, tier in enumerate(tiers):
            cell = QRectF(index * (width + gap) + 0.5, 0.5, width - 1, self.height() - 1)
            if tier == self._winner:
                fill, edge, ink = QColor(106, 169, 255, 36), QColor(ACCENT), QColor(TEXT)
            elif tier in self._live:
                fill, edge, ink = QColor(255, 255, 255, 8), QColor(255, 255, 255, 40), QColor(TEXT)
            else:
                fill, edge, ink = QColor(0, 0, 0, 0), QColor(255, 255, 255, 13), QColor(DIM)
            painter.setPen(QPen(edge, 1))
            painter.setBrush(fill)
            painter.drawRoundedRect(cell, 4, 4)
            painter.setPen(ink)
            painter.setFont(mono_font(12, QFont.Weight.DemiBold))
            painter.drawText(QRectF(cell.x(), cell.y() + 5, cell.width(), 16), Qt.AlignmentFlag.AlignCenter, f"P{int(tier)}")
            painter.setFont(ui_font(10))
            painter.drawText(QRectF(cell.x(), cell.y() + 21, cell.width(), 14), Qt.AlignmentFlag.AlignCenter, tier.label)


class DiscordPreview(QWidget):
    def __init__(self, app_name: str, parent=None) -> None:
        super().__init__(parent)
        self._app_name = app_name
        self._start: float | None = None
        self._end: float | None = None
        self._paused = False
        self._connection = ("disconnected", "")
        self._resolution: Resolution | None = None
        self._buttons: list[QPushButton] = []

        user = getpass.getuser()
        card = QFrame(objectName="DiscordCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)
        card_layout.addWidget(ProfileHeader(user))

        identity = QVBoxLayout()
        identity.setContentsMargins(16, 0, 16, 12)
        identity.setSpacing(0)
        name = QLabel(user, objectName="DcName")
        name.setFont(ui_font(18, QFont.Weight.Bold))
        handle = QLabel(user.lower(), objectName="DcHandle")
        handle.setFont(ui_font(13))
        identity.addWidget(name)
        identity.addWidget(handle)
        card_layout.addLayout(identity)

        self._box = QFrame(objectName="DiscordBox")
        box_layout = QVBoxLayout(self._box)
        box_layout.setContentsMargins(14, 12, 14, 14)
        box_layout.setSpacing(10)
        self._section = QLabel("", objectName="DcSection")
        self._section.setFont(ui_font(12, QFont.Weight.DemiBold))
        box_layout.addWidget(self._section)

        self._row = QWidget()
        row = QHBoxLayout(self._row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self._art = ArtworkTile()
        row.addWidget(self._art, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(1)
        self._details = ElidedLabel(objectName="DcDetails")
        self._details.setFont(ui_font(14, QFont.Weight.DemiBold))
        self._state = ElidedLabel(objectName="DcState")
        self._state.setFont(ui_font(13))
        self._clock = ElidedLabel(objectName="DcClock")
        self._clock.setFont(ui_font(13))
        for label in (self._details, self._state, self._clock):
            text.addWidget(label)
        text.addStretch(1)
        row.addLayout(text, 1)
        box_layout.addWidget(self._row)

        self._progress_wrap = QWidget()
        progress = QVBoxLayout(self._progress_wrap)
        progress.setContentsMargins(0, 0, 0, 0)
        progress.setSpacing(4)
        self._track = ProgressTrack()
        progress.addWidget(self._track)
        times = QHBoxLayout()
        self._time_left = QLabel("00:00", objectName="DcClock")
        self._time_right = QLabel("00:00", objectName="DcClock")
        for label in (self._time_left, self._time_right):
            label.setFont(ui_font(11))
        times.addWidget(self._time_left)
        times.addStretch(1)
        times.addWidget(self._time_right)
        progress.addLayout(times)
        box_layout.addWidget(self._progress_wrap)

        self._button_host = QVBoxLayout()
        self._button_host.setSpacing(8)
        box_layout.addLayout(self._button_host)

        self._empty = QLabel("", objectName="DcEmpty")
        self._empty.setWordWrap(True)
        self._empty.setFont(ui_font(13))
        box_layout.addWidget(self._empty)

        self._fade = QGraphicsOpacityEffect(self._box)
        self._box.setGraphicsEffect(self._fade)
        box_wrap = QVBoxLayout()
        box_wrap.setContentsMargins(12, 0, 12, 12)
        box_wrap.addWidget(self._box)
        card_layout.addLayout(box_wrap)

        self._note = QLabel("", objectName="PreviewNote")
        self._note.setWordWrap(True)
        self._note.setFont(ui_font(12))
        self._strip = TierStrip()
        self._reason = QLabel("", objectName="PreviewReason")
        self._reason.setWordWrap(True)
        self._reason.setFont(mono_font(11))

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.addWidget(card)
        root.addWidget(self._note)
        root.addWidget(self._strip)
        root.addWidget(self._reason)
        root.addStretch(1)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self.set_resolution(None)
        self.set_connection("disconnected", "Starting")

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._refresh_note()
        self._fade.setOpacity(0.45 if paused else 1.0)

    def set_connection(self, status: str, detail: str) -> None:
        self._connection = (status, detail)
        self._refresh_note()

    def set_resolution(self, resolution: Resolution | None) -> None:
        self._resolution = resolution
        if resolution is None:
            self._show_empty("Waiting for the first scan of running applications.")
            self._strip.set_state(None, frozenset())
            self._reason.setText("")
            return
        self._strip.set_state(resolution.tier, resolution.live_tiers)
        self._reason.setText(resolution.reason)
        if resolution.presence is None:
            self._show_empty(
                "Nothing to show. Standby presence is off (show_idle_presence in settings.json) "
                "and no application matched a rule."
            )
        else:
            self._show_presence(resolution.presence)
        self._refresh_note()

    def _show_empty(self, text: str) -> None:
        self._section.setText("No activity")
        self._row.hide()
        self._progress_wrap.hide()
        self._clear_buttons()
        self._empty.setText(text)
        self._empty.show()
        self._start = self._end = None

    def _show_presence(self, presence: PresenceData) -> None:
        verb = TYPE_VERBS.get(presence.activity_type, "Playing")
        self._section.setText(f"{verb} {self._app_name}")
        self._empty.hide()
        self._row.show()
        self._art.set_images(presence.large_image, presence.small_image, presence.large_text, presence.small_text)
        self._details.setText(presence.details or "")
        self._details.setVisible(bool(presence.details))
        self._state.setText(presence.state or "")
        self._state.setVisible(bool(presence.state))
        self._start, self._end = presence.start, presence.end
        self._clock.setVisible(self._start is not None and self._end is None)
        self._progress_wrap.setVisible(self._start is not None and self._end is not None)
        self._clear_buttons()
        for label, url in presence.buttons:
            button = QPushButton(label, objectName="DiscordButton")
            button.setFont(ui_font(13, QFont.Weight.DemiBold))
            button.setToolTip(url)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, target=url: QDesktopServices.openUrl(QUrl(target)))
            self._button_host.addWidget(button)
            self._buttons.append(button)
        self._tick()

    def _clear_buttons(self) -> None:
        for button in self._buttons:
            self._button_host.removeWidget(button)
            button.deleteLater()
        self._buttons.clear()

    def _tick(self) -> None:
        if self._start is None:
            return
        now = time.time()
        elapsed = now - self._start
        if self._end is None:
            self._clock.setText(f"{format_clock(elapsed)} elapsed")
            return
        total = max(self._end - self._start, 1.0)
        self._track.set_fraction(elapsed / total)
        self._time_left.setText(format_clock(min(elapsed, total)))
        self._time_right.setText(format_clock(total))

    def _refresh_note(self) -> None:
        status, detail = self._connection
        if self._paused:
            text, color = "Paused. The card below is a preview and nothing is sent to Discord.", MUTED
        elif status == "connected":
            text, color = "Live on Discord. This card matches what others see.", MUTED
        else:
            text = f"Not sent to Discord: {detail or status}. The card shows what would be sent."
            color = STATUS_COLORS.get(status, MUTED)
        self._note.setText(text)
        self._note.setStyleSheet(f"color: {color};")

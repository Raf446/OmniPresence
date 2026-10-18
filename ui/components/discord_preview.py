"""Live mockup of the Discord profile card with banner, avatar status ring, smooth media progress, and color-coded priority tier pills."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.priority_engine import PresenceData, Resolution, Tier
from ui import (
    DIM,
    MUTED,
    STATUS_COLORS,
    TEXT,
    TIER_COLORS,
    TIER_TITLES,
    mono_font,
    tier_color,
    ui_font,
)

DC_CARD = "#13161d"
DC_BOX = "#1b1f27"
DC_BANNER_BASE = "#1c2230"
DC_TILE = "#252b37"
DC_BADGE = "#323948"
DC_ONLINE = "#00e676"
DC_TEXT = "#f4f7fc"
DC_MUTED = "#adb8cc"
DC_TRACK = "#323946"

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
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b"User-Agent", b"OmniPresence/2.0")
        req.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        reply = self._manager.get(req)
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
    """Mini gradient banner + rounded avatar with status ring and active tier pill."""

    BANNER = 68
    AVATAR = 72

    def __init__(self, name: str = "Discord User", parent=None) -> None:
        super().__init__(parent)
        self._initial = (name.strip()[:1] or "D").upper()
        self._accent = QColor(TIER_COLORS[4])
        self._tier_badge = "STANDBY"
        self._ipc_status = "disconnected"
        self.setFixedHeight(self.BANNER + self.AVATAR // 2 + 6)
        self.setAccessibleName("Profile header preview")

    def set_name(self, name: str) -> None:
        cleaned = (name or "").strip()
        self._initial = (cleaned[:1] or "D").upper()
        self.update()

    def set_tier(self, tier: Tier | None) -> None:
        if tier is None:
            self._accent = QColor(TIER_COLORS[6])
            self._tier_badge = "STANDBY"
        else:
            self._accent = tier_color(int(tier))
            self._tier_badge = f"P{int(tier)} \u00b7 {tier.label.upper()}"
        self.update()

    def set_ipc_status(self, status: str) -> None:
        self._ipc_status = status
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Rounded top banner with layered tier accent glow
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, self.width(), self.BANNER + 36), 11, 11)
        painter.setClipPath(clip)

        banner_rect = QRectF(0, 0, self.width(), self.BANNER)
        grad = QLinearGradient(0, 0, self.width(), self.BANNER)
        c_top = QColor(self._accent)
        c_top.setAlpha(95)
        c_mid = QColor(DC_BANNER_BASE)
        grad.setColorAt(0.0, c_top)
        grad.setColorAt(0.65, c_mid)
        grad.setColorAt(1.0, QColor("#151922"))
        painter.fillRect(banner_rect, grad)

        # Radial highlight in top-right of banner
        radial = QRadialGradient(QPointF(self.width() * 0.85, 12), self.width() * 0.55)
        glow_c = QColor(self._accent)
        glow_c.setAlpha(70)
        radial.setColorAt(0.0, glow_c)
        glow_zero = QColor(self._accent)
        glow_zero.setAlpha(0)
        radial.setColorAt(1.0, glow_zero)
        painter.fillRect(banner_rect, radial)

        # Bottom banner hairline
        painter.setPen(QPen(QColor(255, 255, 255, 24), 1))
        painter.drawLine(0, self.BANNER, self.width(), self.BANNER)

        # Tier pill inside top-right of banner
        painter.setFont(mono_font(10, QFont.Weight.DemiBold))
        fm = QFontMetrics(painter.font())
        pill_w = fm.horizontalAdvance(self._tier_badge) + 18
        pill_rect = QRectF(self.width() - pill_w - 12, 12, pill_w, 20)
        pill_bg = QColor(12, 14, 18, 185)
        pill_border = QColor(self._accent)
        pill_border.setAlpha(150)
        painter.setPen(QPen(pill_border, 1))
        painter.setBrush(pill_bg)
        painter.drawRoundedRect(pill_rect, 10, 10)
        painter.setPen(QColor(DC_TEXT))
        painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, self._tier_badge)

        painter.setClipping(False)

        # Avatar cutout + ring + gradient circle
        cx, cy = 16 + self.AVATAR / 2 + 6, self.BANNER + 2
        ring = self.AVATAR + 12
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(DC_CARD))
        painter.drawEllipse(QPointF(cx, cy), ring / 2, ring / 2)

        av_grad = QLinearGradient(cx - self.AVATAR / 2, cy - self.AVATAR / 2, cx + self.AVATAR / 2, cy + self.AVATAR / 2)
        av_top = QColor(self._accent).darker(125)
        av_bot = QColor("#252b38")
        av_grad.setColorAt(0.0, av_top)
        av_grad.setColorAt(1.0, av_bot)
        ring_pen = QPen(QColor(self._accent), 1.6)
        painter.setPen(ring_pen)
        painter.setBrush(av_grad)
        painter.drawEllipse(QPointF(cx, cy), self.AVATAR / 2, self.AVATAR / 2)

        painter.setPen(QColor(DC_TEXT))
        painter.setFont(ui_font(28, QFont.Weight.Bold))
        painter.drawText(QRectF(cx - 40, cy - 40, 80, 80), Qt.AlignmentFlag.AlignCenter, self._initial)

        # Status dot with outer cutout frame and radial glow
        dot = QPointF(cx + self.AVATAR / 2 - 8, cy + self.AVATAR / 2 - 8)
        status_col = QColor(STATUS_COLORS.get(self._ipc_status, DC_ONLINE))
        if self._ipc_status == "connected":
            status_col = QColor(DC_ONLINE)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(DC_CARD))
        painter.drawEllipse(dot, 13, 13)

        dot_glow = QRadialGradient(dot, 12)
        glow_start = QColor(status_col)
        glow_start.setAlpha(120)
        dot_glow.setColorAt(0.0, glow_start)
        glow_end = QColor(status_col)
        glow_end.setAlpha(0)
        dot_glow.setColorAt(1.0, glow_end)
        painter.setBrush(dot_glow)
        painter.drawEllipse(dot, 12, 12)

        painter.setBrush(status_col)
        painter.drawEllipse(dot, 8.5, 8.5)


class ArtworkTile(QWidget):
    """Large image with the small badge overlapping its corner, as Discord lays them out."""

    LARGE = 62

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(70, 70)
        self._large = ""
        self._small = ""
        self._accent = QColor(TIER_COLORS[4])
        self.setAccessibleName("Presence artwork preview")

    def set_accent(self, color: QColor) -> None:
        self._accent = color
        self.update()

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

    _ASSET_GLYPHS = {
        "celestial_night": "\U0001f319",
        "celestial_day": "\u2600",
        "celestial_dawn": "\U0001f305",
        "celestial_golden": "\U0001f307",
        "celestial_dusk": "\U0001f306",
        "weather_rain": "\U0001f327",
        "weather_storm": "\u26c8",
        "ram_chip": "RM",
        "cpu_chip": "CP",
        "shield": "\U0001f6e1",
        "lock": "\U0001f512",
        "mood_neon": "\u26a1",
        "mood_cyber": "\U0001f30c",
        "mood_lofi": "\u2615",
        "mood_mono": "\U0001f5a4",
    }

    @classmethod
    def _glyph(cls, key: str, length: int) -> str:
        if key.lower().startswith(("http://", "https://")):
            return ""
        mapped = cls._ASSET_GLYPHS.get(key.lower())
        if mapped:
            return mapped[:length]
        return (key[:length] or "OP").upper()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        tile = QRectF(0, 0, self.LARGE, self.LARGE)
        path = QPainterPath()
        path.addRoundedRect(tile, 10, 10)

        tile_grad = QLinearGradient(0, 0, self.LARGE, self.LARGE)
        c1 = QColor(self._accent)
        c1.setAlpha(48)
        tile_grad.setColorAt(0.0, c1)
        tile_grad.setColorAt(1.0, QColor(DC_TILE))

        border_col = QColor(self._accent)
        border_col.setAlpha(90)
        painter.setPen(QPen(border_col, 1))
        painter.setBrush(tile_grad)
        painter.drawPath(path)

        large = self._pixmap(self._large)
        if large is not None:
            painter.setClipPath(path)
            scaled = large.scaled(
                self.LARGE,
                self.LARGE,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawPixmap(0, 0, scaled)
            painter.setClipping(False)
        else:
            painter.setPen(QColor(DC_TEXT))
            painter.setFont(mono_font(15, QFont.Weight.Bold))
            painter.drawText(tile, Qt.AlignmentFlag.AlignCenter, self._glyph(self._large, 2))

        if self._small:
            center = QPointF(self.LARGE - 5, self.LARGE - 5)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(DC_BOX))
            painter.drawEllipse(center, 14, 14)
            small = self._pixmap(self._small)
            badge = QPainterPath()
            badge.addEllipse(center, 11, 11)
            painter.setBrush(QColor(DC_BADGE))
            painter.setPen(QPen(QColor(self._accent), 1))
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
                painter.setPen(QColor(DC_TEXT))
                painter.setFont(mono_font(9, QFont.Weight.Bold))
                painter.drawText(
                    QRectF(center.x() - 11, center.y() - 11, 22, 22),
                    Qt.AlignmentFlag.AlignCenter,
                    self._glyph(self._small, 1),
                )


class ProgressTrack(QWidget):
    """Smooth animated media duration progress bar with accent gradient and playhead."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(8)
        self._fraction = 0.0
        self._target = 0.0
        self._accent = QColor(TIER_COLORS[5])

    def set_accent(self, color: QColor) -> None:
        self._accent = color
        self.update()

    def set_fraction(self, fraction: float) -> None:
        clamped = min(max(fraction, 0.0), 1.0)
        self._target = clamped
        if abs(self._target - self._fraction) > 0.15:
            self._fraction = self._target
        else:
            self._fraction += (self._target - self._fraction) * 0.35
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        track_y = 2.0
        track_h = 4.0
        painter.setBrush(QColor(DC_TRACK))
        painter.drawRoundedRect(QRectF(0, track_y, self.width(), track_h), 2, 2)

        fill_w = max(4.0, self.width() * self._fraction) if self._fraction > 0 else 0.0
        if fill_w > 0:
            grad = QLinearGradient(0, 0, fill_w, 0)
            grad.setColorAt(0.0, self._accent)
            grad.setColorAt(1.0, QColor(DC_TEXT))
            painter.setBrush(grad)
            painter.drawRoundedRect(QRectF(0, track_y, fill_w, track_h), 2, 2)

            knob_x = min(max(fill_w, 3.0), self.width() - 3.0)
            painter.setBrush(QColor(DC_TEXT))
            painter.drawEllipse(QPointF(knob_x, track_y + track_h / 2), 3.2, 3.2)


class TierStrip(QWidget):
    """Six color-coded pill badges for P1..P6 with pulsating indicator on P2 (Compiler Active)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(48)
        self._winner: Tier | None = None
        self._live: frozenset[Tier] = frozenset()
        self._phase = 0.0
        self.setAccessibleName("Priority tiers")

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(60)
        self._pulse_timer.timeout.connect(self._advance_pulse)
        self._pulse_timer.start()

    def _advance_pulse(self) -> None:
        self._phase = (self._phase + 0.18) % (math.pi * 2)
        if self._winner == Tier.BUILD or Tier.BUILD in self._live:
            self.update()

    def set_state(self, winner: Tier | None, live: frozenset[Tier]) -> None:
        self._winner, self._live = winner, live
        names = ", ".join(f"P{int(t)} {TIER_TITLES.get(int(t), t.label)}" for t in sorted(live)) or "none"
        self.setToolTip(f"Active Priority Hierarchy | Live candidates: {names}")
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        tiers = list(Tier)
        gap = 5
        width = (self.width() - gap * (len(tiers) - 1)) / len(tiers)
        pulse = 0.5 + 0.5 * math.sin(self._phase)

        for index, tier in enumerate(tiers):
            cell = QRectF(index * (width + gap) + 0.5, 1.0, width - 1, self.height() - 2.0)
            base_color = tier_color(int(tier))
            is_winner = tier == self._winner
            is_live = tier in self._live

            if is_winner:
                fill = QColor(base_color)
                fill.setAlpha(58 if tier != Tier.BUILD else int(45 + 45 * pulse))
                edge = QColor(base_color)
                edge.setAlpha(235)
                ink = QColor(TEXT)
                sub_ink = QColor(base_color).lighter(135)
                pen_w = 1.5
            elif is_live:
                fill = QColor(base_color)
                fill.setAlpha(26)
                edge = QColor(base_color)
                edge.setAlpha(130)
                ink = QColor(base_color).lighter(125)
                sub_ink = QColor(MUTED)
                pen_w = 1.1
            else:
                fill = QColor(20, 24, 32, 180)
                edge = QColor(base_color)
                edge.setAlpha(55)
                ink = QColor(base_color)
                ink.setAlpha(175)
                sub_ink = QColor(DIM)
                pen_w = 1.0

            painter.setPen(QPen(edge, pen_w))
            painter.setBrush(fill)
            painter.drawRoundedRect(cell, 8, 8)

            # Top accent color dot inside each pill (pulsating for P2 Compiler Active)
            dot_center = QPointF(cell.right() - 8, cell.top() + 8)
            dot_color = QColor(base_color)
            if tier == Tier.BUILD and (is_winner or is_live):
                halo = QRadialGradient(dot_center, 7.0)
                hc = QColor(base_color)
                hc.setAlpha(int(180 * pulse))
                halo.setColorAt(0.0, hc)
                hc_end = QColor(base_color)
                hc_end.setAlpha(0)
                halo.setColorAt(1.0, hc_end)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(halo)
                painter.drawEllipse(dot_center, 7.0, 7.0)
            dot_color.setAlpha(255 if (is_winner or is_live) else 130)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(dot_color)
            painter.drawEllipse(dot_center, 2.8, 2.8)

            painter.setPen(ink)
            painter.setFont(mono_font(11, QFont.Weight.Bold))
            painter.drawText(
                QRectF(cell.x() + 4, cell.y() + 5, cell.width() - 10, 16),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"P{int(tier)}",
            )
            painter.setPen(sub_ink)
            painter.setFont(ui_font(10, QFont.Weight.DemiBold if is_winner else QFont.Weight.Normal))
            painter.drawText(
                QRectF(cell.x() + 2, cell.y() + 23, cell.width() - 4, 16),
                Qt.AlignmentFlag.AlignCenter,
                tier.label,
            )


class DiscordPreview(QWidget):
    def __init__(
        self,
        app_name: str,
        display_name: str = "Discord User",
        handle: str = "@discord",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._app_name = app_name
        self._start: float | None = None
        self._end: float | None = None
        self._paused = False
        self._connection = ("disconnected", "")
        self._resolution: Resolution | None = None
        self._buttons: list[QPushButton] = []

        self._card = QFrame(objectName="DiscordCard")
        self._card_shadow = QGraphicsDropShadowEffect(self._card)
        self._card_shadow.setBlurRadius(24)
        self._card_shadow.setOffset(0, 4)
        self._card_shadow.setColor(QColor(41, 121, 255, 45))
        self._card.setGraphicsEffect(self._card_shadow)

        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)
        self._header = ProfileHeader(display_name)
        card_layout.addWidget(self._header)

        identity = QVBoxLayout()
        identity.setContentsMargins(16, 2, 16, 10)
        identity.setSpacing(2)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        self._name_label = ElidedLabel(objectName="DcName")
        self._name_label.setFont(ui_font(17, QFont.Weight.Bold))
        name_row.addWidget(self._name_label, 1)
        self._role_pill = QLabel("Rich Presence", objectName="DcBadgePill")
        self._role_pill.setFont(mono_font(10, QFont.Weight.DemiBold))
        name_row.addWidget(self._role_pill, 0)
        identity.addLayout(name_row)

        self._handle_label = ElidedLabel(objectName="DcHandle")
        self._handle_label.setFont(ui_font(12))
        identity.addWidget(self._handle_label)
        card_layout.addLayout(identity)

        self._box = QFrame(objectName="DiscordBox")
        box_layout = QVBoxLayout(self._box)
        box_layout.setContentsMargins(14, 12, 14, 14)
        box_layout.setSpacing(9)
        self._section = QLabel("", objectName="DcSection")
        self._section.setFont(ui_font(11, QFont.Weight.Bold))
        box_layout.addWidget(self._section)

        self._row = QWidget()
        row = QHBoxLayout(self._row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self._art = ArtworkTile()
        row.addWidget(self._art, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(2)
        self._details = ElidedLabel(objectName="DcDetails")
        self._details.setFont(ui_font(13, QFont.Weight.DemiBold))
        self._state = ElidedLabel(objectName="DcState")
        self._state.setFont(ui_font(12))
        self._clock = ElidedLabel(objectName="DcClock")
        self._clock.setFont(mono_font(11))
        for label in (self._details, self._state, self._clock):
            text.addWidget(label)
        text.addStretch(1)
        row.addLayout(text, 1)
        box_layout.addWidget(self._row)

        self._progress_wrap = QWidget()
        progress = QVBoxLayout(self._progress_wrap)
        progress.setContentsMargins(0, 2, 0, 0)
        progress.setSpacing(4)
        self._track = ProgressTrack()
        progress.addWidget(self._track)
        times = QHBoxLayout()
        self._time_left = QLabel("00:00", objectName="DcClock")
        self._time_right = QLabel("00:00", objectName="DcClock")
        for label in (self._time_left, self._time_right):
            label.setFont(mono_font(10))
        times.addWidget(self._time_left)
        times.addStretch(1)
        times.addWidget(self._time_right)
        progress.addLayout(times)
        box_layout.addWidget(self._progress_wrap)

        self._button_host = QVBoxLayout()
        self._button_host.setSpacing(6)
        box_layout.addLayout(self._button_host)

        self._empty = QLabel("", objectName="DcEmpty")
        self._empty.setWordWrap(True)
        self._empty.setFont(ui_font(12))
        box_layout.addWidget(self._empty)

        self._fade = QGraphicsOpacityEffect(self._box)
        self._box.setGraphicsEffect(self._fade)
        box_wrap = QVBoxLayout()
        box_wrap.setContentsMargins(12, 0, 12, 12)
        box_wrap.addWidget(self._box)
        card_layout.addLayout(box_wrap)

        self._note = QLabel("", objectName="PreviewNote")
        self._note.setWordWrap(True)
        self._note.setFont(ui_font(11))
        self._strip = TierStrip()
        self._reason = QLabel("", objectName="PreviewReason")
        self._reason.setWordWrap(True)
        self._reason.setFont(mono_font(11))

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(9)
        root.addWidget(self._card)
        root.addWidget(self._note)
        root.addWidget(self._strip)
        root.addWidget(self._reason)
        root.addStretch(1)

        # 100ms tick for smooth media progress bar interpolation
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self.set_profile(display_name, handle)
        self.set_resolution(None)
        self.set_connection("disconnected", "Starting")

    def set_profile(self, display_name: str, handle: str) -> None:
        """Reactively update the profile display name, handle, and avatar initial."""
        clean_name = (display_name or "").strip() or "Discord User"
        clean_handle = (handle or "").strip() or "@discord"
        if not clean_handle.startswith("@"):
            clean_handle = f"@{clean_handle}"
        self._header.set_name(clean_name)
        self._name_label.setText(clean_name)
        self._handle_label.setText(f"{clean_handle} \u00b7 OmniPresence Engine")

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._refresh_note()
        self._fade.setOpacity(0.45 if paused else 1.0)

    def set_connection(self, status: str, detail: str) -> None:
        self._connection = (status, detail)
        self._header.set_ipc_status(status)
        self._refresh_note()

    def set_resolution(self, resolution: Resolution | None) -> None:
        self._resolution = resolution
        if resolution is None:
            self._header.set_tier(None)
            self._show_empty("Waiting for the first scan of running applications.")
            self._strip.set_state(None, frozenset())
            self._reason.setText("Arbitration engine initializing...")
            return

        self._header.set_tier(resolution.tier)
        if resolution.tier is not None:
            accent = tier_color(int(resolution.tier))
            glow = QColor(accent)
            glow.setAlpha(55)
            self._card_shadow.setColor(glow)
            self._art.set_accent(accent)
            self._track.set_accent(accent)
            self._role_pill.setText(TIER_TITLES.get(int(resolution.tier), resolution.tier.label))

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
        self._section.setText("NO ACTIVE PRESENCE")
        self._row.hide()
        self._progress_wrap.hide()
        self._clear_buttons()
        self._empty.setText(text)
        self._empty.show()
        self._start = self._end = None

    def _show_presence(self, presence: PresenceData) -> None:
        verb = TYPE_VERBS.get(presence.activity_type, "Playing")
        self._section.setText(f"{verb.upper()} {self._app_name.upper()}")
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
            button.setFont(ui_font(12, QFont.Weight.DemiBold))
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
        elapsed = max(0.0, now - self._start)
        if self._end is None:
            self._clock.setText(f"\u23f1 {format_clock(elapsed)} elapsed")
            return
        total = max(self._end - self._start, 1.0)
        self._track.set_fraction(elapsed / total)
        self._time_left.setText(format_clock(min(elapsed, total)))
        self._time_right.setText(format_clock(total))

    def _refresh_note(self) -> None:
        status, detail = self._connection
        if self._paused:
            text, color = "Paused \u2014 preview active locally, IPC sync suspended.", MUTED
        elif status == "connected":
            text, color = "\u25cf Live on Discord IPC \u2014 synced in real time.", STATUS_COLORS["connected"]
        else:
            text = f"\u25cb Offline preview ({detail or status}) \u2014 waiting for Discord IPC."
            color = STATUS_COLORS.get(status, MUTED)
        self._note.setText(text)
        self._note.setStyleSheet(f"color: {color};")

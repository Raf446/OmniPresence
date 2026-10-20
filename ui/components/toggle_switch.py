"""Modern iOS/macOS-style animated pill ToggleSwitch for OmniPresence v2.4."""
from __future__ import annotations

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget


class ToggleSwitch(QWidget):
    """Smoothly animated pill toggle switch with #272C3E OFF track and Emerald/Cyan ON gradient."""

    toggled = Signal(bool)

    def __init__(self, checked: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._checked = bool(checked)
        self._thumb_pos = 1.0 if self._checked else 0.0
        self._hovered = False

        self.setFixedSize(46, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._anim = QPropertyAnimation(self, b"thumb_pos", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(46, 24)

    def isChecked(self) -> bool:  # noqa: N802
        return self._checked

    def setChecked(self, checked: bool, *, animate: bool = True) -> None:  # noqa: N802
        target = bool(checked)
        if target == self._checked:
            return
        self._checked = target
        end_val = 1.0 if target else 0.0
        self._anim.stop()
        if animate and self.isVisible():
            self._anim.setStartValue(self._thumb_pos)
            self._anim.setEndValue(end_val)
            self._anim.start()
        else:
            self._thumb_pos = end_val
            self.update()
        self.toggled.emit(self._checked)

    def _get_thumb_pos(self) -> float:
        return self._thumb_pos

    def _set_thumb_pos(self, pos: float) -> None:
        self._thumb_pos = min(max(float(pos), 0.0), 1.0)
        self.update()

    thumb_pos = Property(float, _get_thumb_pos, _set_thumb_pos)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.setChecked(not self._checked, animate=True)
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.isEnabled():
            self.setChecked(not self._checked, animate=True)
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)
        radius = rect.height() / 2.0

        # Interpolate OFF (#272C3E) and ON (#10B981 -> #38BDF8) track colors
        t = self._thumb_pos
        if t <= 0.02:
            painter.setBrush(QColor("#272C3E"))
            border_col = QColor(255, 255, 255, 34 if self._hovered else 20)
        else:
            grad = QLinearGradient(rect.topLeft(), rect.topRight())
            c_off = QColor("#272C3E")
            c_start = QColor("#10B981")
            c_end = QColor("#38BDF8")
            r1 = int(c_off.red() + (c_start.red() - c_off.red()) * t)
            g1 = int(c_off.green() + (c_start.green() - c_off.green()) * t)
            b1 = int(c_off.blue() + (c_start.blue() - c_off.blue()) * t)
            r2 = int(c_off.red() + (c_end.red() - c_off.red()) * t)
            g2 = int(c_off.green() + (c_end.green() - c_off.green()) * t)
            b2 = int(c_off.blue() + (c_end.blue() - c_off.blue()) * t)
            grad.setColorAt(0.0, QColor(r1, g1, b1))
            grad.setColorAt(1.0, QColor(r2, g2, b2))
            painter.setBrush(grad)
            border_col = QColor(56, 189, 248, int(90 * t) + 25)

        painter.setPen(QPen(border_col, 1.0))
        painter.drawRoundedRect(rect, radius, radius)

        # Thumb knob
        margin = 3.0
        knob_d = self.height() - (margin * 2.0)
        min_x = margin
        max_x = self.width() - margin - knob_d
        knob_x = min_x + (max_x - min_x) * t
        knob_rect = QRectF(knob_x, margin, knob_d, knob_d)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 55))
        painter.drawEllipse(knob_rect.translated(0.0, 1.0))

        painter.setBrush(QColor("#F8FAFC") if self.isEnabled() else QColor("#94A3B8"))
        painter.drawEllipse(knob_rect)

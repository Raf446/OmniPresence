"""Shared UI tokens: palette, font stacks and the generated application icon."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPen, QPixmap

BG = "#0a0c10"
SURFACE = "#12151b"
SURFACE_RAISED = "#181c24"
BORDER = "rgba(255, 255, 255, 13)"
BORDER_STRONG = "rgba(255, 255, 255, 26)"
TEXT = "#e6e8ec"
MUTED = "#8b93a1"
DIM = "#7a8392"
ACCENT = "#6aa9ff"
SUCCESS = "#3fb97f"
WARN = "#d9a441"
DANGER = "#f0616d"

STATUS_COLORS = {
    "connected": SUCCESS,
    "connecting": WARN,
    "disconnected": DANGER,
    "error": DANGER,
}

# Labels and body copy read in a proportional face; data (timestamps, ids, tiers, logs) is
# monospaced so digits and identifiers line up. JetBrains Mono leads, as the brief asks.
UI_FAMILIES = ("Inter", "Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI")
MONO_FAMILIES = ("JetBrains Mono", "Cascadia Mono", "Consolas")


def _installed(candidates: tuple[str, ...], fallback: str) -> list[str]:
    available = set(QFontDatabase.families())
    chosen = [name for name in candidates if name in available]
    return chosen or [fallback]


def _font(families: list[str], pixel_size: int, weight: QFont.Weight) -> QFont:
    font = QFont()
    font.setFamilies(families)
    font.setPixelSize(pixel_size)
    font.setWeight(weight)
    return font


def ui_font(pixel_size: int = 13, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    return _font(_installed(UI_FAMILIES, "Segoe UI"), pixel_size, weight)


def mono_font(pixel_size: int = 12, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    return _font(_installed(MONO_FAMILIES, "Consolas"), pixel_size, weight)


def app_icon(status: str = "disconnected") -> QIcon:
    """A ring with a centre dot: the dot colour is the live Discord connection state."""
    icon = QIcon()
    dot = QColor(STATUS_COLORS.get(status, DANGER))
    for size in (16, 24, 32, 48, 64):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(SURFACE))
        painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
        margin = size * 0.2
        painter.setPen(QPen(QColor(TEXT), max(1.4, size * 0.09)))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(margin, margin, size - 2 * margin, size - 2 * margin))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(dot)
        diameter = size * 0.26
        painter.drawEllipse(QRectF((size - diameter) / 2, (size - diameter) / 2, diameter, diameter))
        painter.end()
        icon.addPixmap(pixmap)
    return icon

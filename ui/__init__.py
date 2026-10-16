"""Shared UI tokens: layered palette, tier colors, font stacks, resource resolver, and app icon."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QRadialGradient,
)

# Layered dark mode palette
BG = "#0c0e12"
SURFACE = "#141820"
SURFACE_RAISED = "#1b212c"
BORDER = "rgba(255, 255, 255, 0.08)"
BORDER_STRONG = "rgba(255, 255, 255, 0.16)"
TEXT = "#eef2f8"
MUTED = "#94a0b8"
DIM = "#6c788e"
ACCENT = "#2979ff"
SUCCESS = "#00e676"
WARN = "#ffca28"
DANGER = "#ff4d4d"

STATUS_COLORS: dict[str, str] = {
    "connected": "#00e676",     # Pulsating Emerald Green
    "connecting": "#ffca28",    # Pulsating Amber Yellow
    "reconnecting": "#ffca28",  # Pulsating Amber Yellow
    "disconnected": "#78909c",  # Muted Slate / Red-Grey
    "error": "#ff4d4d",         # Crimson Red
}

# Exact Pill Badge colors for Priority Tiers P1..P6
TIER_COLORS: dict[int, str] = {
    1: "#ff4d4d",  # P1 Manual Override (Crimson Red)
    2: "#ff9800",  # P2 Compiler Active (Amber Orange + pulse)
    3: "#00e676",  # P3 Gaming Mode (Emerald Green)
    4: "#2979ff",  # P4 IDE / Git Context (Sapphire Blue)
    5: "#b388ff",  # P5 Media / Music Playing (Violet Purple)
    6: "#78909c",  # P6 Idle / AFK (Muted Slate)
}

TIER_TITLES: dict[int, str] = {
    1: "Manual Override",
    2: "Compiler Active",
    3: "Gaming Mode",
    4: "IDE / Git Context",
    5: "Media Playing",
    6: "Idle / AFK",
}

UI_FAMILIES = ("Inter", "Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI")
MONO_FAMILIES = ("JetBrains Mono", "Cascadia Mono", "Consolas")


def resource_path(relative_path: str | Path) -> Path:
    """Resolve bundled read-only resources for both source runs and PyInstaller (sys._MEIPASS)."""
    meipass = getattr(sys, "_MEIPASS", None)
    base = Path(meipass) if meipass else Path(__file__).resolve().parent.parent
    return (base / relative_path).resolve()


def runtime_config_dir() -> Path:
    """Return writable config directory; seeds defaults from sys._MEIPASS when running as .exe."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        target_dir = exe_dir / "config"
        bundled_dir = resource_path("config")
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            for filename in ("settings.json", "rules.json"):
                dst = target_dir / filename
                src = bundled_dir / filename
                if not dst.exists() and src.exists():
                    shutil.copy2(src, dst)
            return target_dir
        except OSError:
            return bundled_dir
    return resource_path("config")


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


def tier_color(tier_value: int) -> QColor:
    return QColor(TIER_COLORS.get(int(tier_value), TIER_COLORS[6]))


def app_icon(status: str = "disconnected") -> QIcon:
    """Layered rounded tile with outer ring and glowing status core."""
    icon = QIcon()
    dot = QColor(STATUS_COLORS.get(status, DANGER))
    for size in (16, 24, 32, 48, 64, 128):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        bg_grad = QLinearGradient(0, 0, size, size)
        bg_grad.setColorAt(0.0, QColor(SURFACE_RAISED))
        bg_grad.setColorAt(1.0, QColor(BG))
        painter.setPen(QPen(QColor(255, 255, 255, 38), max(1.0, size * 0.04)))
        painter.setBrush(bg_grad)
        painter.drawRoundedRect(QRectF(1, 1, size - 2, size - 2), size * 0.24, size * 0.24)

        margin = size * 0.21
        ring_pen = QPen(QColor(TEXT), max(1.5, size * 0.085))
        painter.setPen(ring_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(margin, margin, size - 2 * margin, size - 2 * margin))

        center = QPointF(size / 2, size / 2)
        glow_radius = size * 0.24
        glow = QRadialGradient(center, glow_radius)
        glow_c = QColor(dot)
        glow_c.setAlpha(140)
        glow.setColorAt(0.0, glow_c)
        glow_end = QColor(dot)
        glow_end.setAlpha(0)
        glow.setColorAt(1.0, glow_end)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(center, glow_radius, glow_radius)

        painter.setBrush(dot)
        diameter = size * 0.26
        painter.drawEllipse(QRectF((size - diameter) / 2, (size - diameter) / 2, diameter, diameter))
        painter.end()
        icon.addPixmap(pixmap)
    return icon

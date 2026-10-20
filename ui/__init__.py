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

# Obsidian Glass v2.4 layered palette
BG = "#0D1117"
SURFACE = "#161B22"
SURFACE_RAISED = "#1A212C"
BORDER = "rgba(255, 255, 255, 0.08)"
BORDER_STRONG = "rgba(0, 229, 255, 0.40)"
TEXT = "#F8FAFC"
MUTED = "#94A3B8"
DIM = "#64748B"
ACCENT = "#00E5FF"
SUCCESS = "#10B981"
WARN = "#F59E0B"
DANGER = "#EF4444"

STATUS_COLORS: dict[str, str] = {
    "connected": "#10B981",     # Pulsating Emerald Green
    "connecting": "#F59E0B",    # Pulsating Amber Yellow
    "reconnecting": "#F59E0B",  # Pulsating Amber Yellow
    "disconnected": "#64748B",  # Muted Slate
    "error": "#EF4444",         # Crimson Red
}

# Exact Pill Badge colors for Priority Tiers P1..P6
TIER_COLORS: dict[int, str] = {
    1: "#EF4444",  # P1 Manual Override / Stealth Shield (Crimson Red)
    2: "#F59E0B",  # P2 Compiler Active (Amber Orange + pulse)
    3: "#10B981",  # P3 Gaming Mode (Emerald Green)
    4: "#00E5FF",  # P4 IDE / Git Control (Cyan / Sapphire Blue)
    5: "#A855F7",  # P5 Media / Music Playing (Violet Purple)
    6: "#64748B",  # P6 Idle / Celestial (Muted Slate)
}

TIER_TITLES: dict[int, str] = {
    1: "Manual / Shield",
    2: "Compiler Active",
    3: "Gaming Mode",
    4: "IDE / Git Control",
    5: "Media Playing",
    6: "Idle / Celestial",
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
            for filename in ("settings.json", "rules.json", "custom_presets.json"):
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


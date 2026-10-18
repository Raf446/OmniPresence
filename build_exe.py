"""Automated PyInstaller build pipeline for OmniPresence standalone Windows executable (.exe)."""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

APP_NAME = "OmniPresence"
APP_VERSION = "2.3.2"
APP_VERSION_TUPLE = (2, 3, 1, 0)
APP_PUBLISHER = "Raf446"
APP_DESCRIPTION = "OmniPresence - Autonomous Multi-Context Discord Rich Presence Engine"

PROJECT_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PROJECT_DIR / "assets"
CONFIG_DIR = PROJECT_DIR / "config"
BUILD_DIR = PROJECT_DIR / "build"
DIST_DIR = PROJECT_DIR / "dist"
ICON_PATH = ASSETS_DIR / "app.ico"
VERSION_FILE = BUILD_DIR / "version_info.txt"


def generate_ico(target_ico: Path) -> Path:
    """Render a crisp multi-resolution Windows .ico file using PySide6 QPainter (no Pillow needed)."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
    from PySide6.QtGui import (
        QColor,
        QGuiApplication,
        QImage,
        QLinearGradient,
        QPainter,
        QPen,
        QRadialGradient,
    )

    app = QGuiApplication.instance() or QGuiApplication([])
    _ = app  # keep reference alive during icon generation

    sizes = (16, 24, 32, 48, 64, 128, 256)
    png_payloads: list[tuple[int, bytes]] = []

    for size in sizes:
        img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)

        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        bg_grad = QLinearGradient(0, 0, size, size)
        bg_grad.setColorAt(0.0, QColor("#1b212c"))
        bg_grad.setColorAt(1.0, QColor("#0c0e12"))
        border_pen = QPen(QColor(41, 121, 255, 160), max(1.0, size * 0.04))
        painter.setPen(border_pen)
        painter.setBrush(bg_grad)
        pad = max(1.0, size * 0.03)
        painter.drawRoundedRect(
            QRectF(pad, pad, size - 2 * pad, size - 2 * pad),
            size * 0.24,
            size * 0.24,
        )

        margin = size * 0.21
        ring_pen = QPen(QColor("#eef2f8"), max(1.5, size * 0.085))
        painter.setPen(ring_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(margin, margin, size - 2 * margin, size - 2 * margin))

        center = QPointF(size / 2, size / 2)
        glow_radius = size * 0.26
        glow = QRadialGradient(center, glow_radius)
        glow.setColorAt(0.0, QColor(0, 230, 118, 190))
        glow.setColorAt(1.0, QColor(0, 230, 118, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(center, glow_radius, glow_radius)

        painter.setBrush(QColor("#00e676"))
        diameter = size * 0.26
        painter.drawEllipse(QRectF((size - diameter) / 2, (size - diameter) / 2, diameter, diameter))
        painter.end()

        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        buf.close()
        png_payloads.append((size, bytes(ba)))

    target_ico.parent.mkdir(parents=True, exist_ok=True)
    header = struct.pack("<HHH", 0, 1, len(png_payloads))
    offset = 6 + 16 * len(png_payloads)
    entries = bytearray()
    blobs = bytearray()

    for size, data in png_payloads:
        dim_byte = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII",
            dim_byte,
            dim_byte,
            0,
            0,
            1,
            32,
            len(data),
            offset,
        )
        blobs += data
        offset += len(data)

    target_ico.write_bytes(header + bytes(entries) + bytes(blobs))
    return target_ico


def write_version_info(target_file: Path) -> Path:
    """Write Windows PE VSVersionInfo resource file for PyInstaller."""
    target_file.parent.mkdir(parents=True, exist_ok=True)
    v1, v2, v3, v4 = APP_VERSION_TUPLE
    content = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({v1}, {v2}, {v3}, {v4}),
    prodvers=({v1}, {v2}, {v3}, {v4}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', '{APP_PUBLISHER}'),
        StringStruct('FileDescription', '{APP_DESCRIPTION}'),
        StringStruct('FileVersion', '{APP_VERSION}.0'),
        StringStruct('InternalName', '{APP_NAME}'),
        StringStruct('LegalCopyright', 'Copyright (c) 2026 {APP_PUBLISHER}'),
        StringStruct('OriginalFilename', '{APP_NAME}.exe'),
        StringStruct('ProductName', '{APP_NAME}'),
        StringStruct('ProductVersion', '{APP_VERSION}'),
        StringStruct('Publisher', '{APP_PUBLISHER}')])
      ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    target_file.write_text(content, encoding="utf-8")
    return target_file


def clean_previous_builds() -> None:
    """Remove old build/, dist/, and .spec artifacts prior to compilation."""
    for folder in (BUILD_DIR, DIST_DIR):
        if folder.exists():
            print(f"[build] Cleaning {folder} ...")
            shutil.rmtree(folder, ignore_errors=True)
    spec_file = PROJECT_DIR / f"{APP_NAME}.spec"
    if spec_file.exists():
        spec_file.unlink()


def build_executable() -> int:
    print("=" * 68)
    print(f" Building {APP_NAME} v{APP_VERSION} (Publisher: {APP_PUBLISHER})")
    print("=" * 68)

    clean_previous_builds()

    print("[build] Generating multi-resolution application icon (assets/app.ico)...")
    icon_file = generate_ico(ICON_PATH)

    print("[build] Generating Windows executable version metadata...")
    version_file = write_version_info(VERSION_FILE)

    sep = ";" if os.name == "nt" else ":"
    entry_script = PROJECT_DIR / "main.py"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--noconsole",
        "--name",
        APP_NAME,
        "--icon",
        str(icon_file),
        "--version-file",
        str(version_file),
        "--paths",
        str(PROJECT_DIR),
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR / "work"),
        "--specpath",
        str(BUILD_DIR),
        "--add-data",
        f"{ASSETS_DIR}{sep}assets",
        "--add-data",
        f"{CONFIG_DIR}{sep}config",
        "--hidden-import",
        "pypresence",
        "--hidden-import",
        "psutil",
        "--hidden-import",
        "PySide6.QtNetwork",
        "--collect-submodules",
        "winsdk",
        str(entry_script),
    ]

    print("[build] Running PyInstaller:")
    print("        " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    result = subprocess.run(cmd, cwd=str(PROJECT_DIR), check=False)
    if result.returncode != 0:
        print(f"[build] ERROR: PyInstaller exited with code {result.returncode}", file=sys.stderr)
        return result.returncode

    exe_path = DIST_DIR / f"{APP_NAME}.exe"
    if not exe_path.is_file():
        print(f"[build] ERROR: Expected output {exe_path} was not created.", file=sys.stderr)
        return 1

    # Seed editable config/ next to dist/OmniPresence.exe for convenient user customization
    dist_config = DIST_DIR / "config"
    dist_config.mkdir(parents=True, exist_ok=True)
    for cfg_name in ("settings.json", "rules.json"):
        src_cfg = CONFIG_DIR / cfg_name
        dst_cfg = dist_config / cfg_name
        if src_cfg.is_file():
            shutil.copy2(src_cfg, dst_cfg)

    size_mb = exe_path.stat().st_size / (1024 * 1024)
    print("=" * 68)
    print(f"[build] SUCCESS! Standalone executable created:")
    print(f"        File      : {exe_path}")
    print(f"        Size      : {size_mb:.2f} MB")
    print(f"        Metadata  : {APP_NAME} v{APP_VERSION} by {APP_PUBLISHER}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(build_executable())

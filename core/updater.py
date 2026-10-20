"""GitHub Releases Auto-Updater & Hot-Swap Binary Replacement for OmniPresence (v2.4)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QApplication

GITHUB_REPO: str = "Raf446/OmniPresence"
LATEST_RELEASE_API_URL: str = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
CURRENT_VERSION: str = "v2.4"
USER_AGENT: str = f"OmniPresence-Updater/{CURRENT_VERSION.lstrip('v')} (+https://github.com/{GITHUB_REPO})"


def parse_semver(version_str: str) -> tuple[int, ...]:
    """Convert a version tag like 'v2.4' or '2.4.0-beta.1' into an integer tuple (2, 4, 0)."""
    cleaned = (version_str or "").strip().lstrip("vV")
    numeric_prefix = re.split(r"[-+]", cleaned, maxsplit=1)[0]
    parts: list[int] = []
    for segment in numeric_prefix.split("."):
        digits = re.sub(r"\D", "", segment)
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer_version(remote_tag: str, local_version: str = CURRENT_VERSION) -> bool:
    """Return True strictly when remote_tag is semantically newer than local_version."""
    try:
        return parse_semver(remote_tag) > parse_semver(local_version)
    except Exception:
        return False


def _select_release_asset(payload: dict) -> tuple[str, str]:
    """Select the preferred binary asset URL (.exe or .zip) from a GitHub Release payload."""
    assets = payload.get("assets")
    if isinstance(assets, list):
        # Priority 1: Direct executable (OmniPresence.exe or Setup .exe)
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "").strip()
            dl_url = str(asset.get("browser_download_url") or "").strip()
            if dl_url and name.lower() == "omnipresence.exe":
                return dl_url, name
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "").strip()
            dl_url = str(asset.get("browser_download_url") or "").strip()
            if dl_url and name.lower().endswith(".exe"):
                return dl_url, name
        # Priority 2: Archive (.zip)
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "").strip()
            dl_url = str(asset.get("browser_download_url") or "").strip()
            if dl_url and name.lower().endswith(".zip"):
                return dl_url, name

    html_url = str(payload.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases/latest").strip()
    return html_url, ""


class UpdateCheckerThread(QThread):
    """Background worker for checking GitHub Releases and downloading update binaries."""

    update_available = Signal(str, str, str)  # (new_version, changelog, download_url)
    download_progress = Signal(int)           # (percent: 0..100)
    download_finished = Signal(str)           # (temp_file_path)
    check_failed = Signal(str)                # (error_message)

    def __init__(
        self,
        current_version: str = CURRENT_VERSION,
        api_url: str = LATEST_RELEASE_API_URL,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._current_version = current_version
        self._api_url = api_url
        self._mode: str = "check"
        self._download_url: str = ""
        self._target_filename: str = "OmniPresence_Update.exe"

    def start_check(self) -> None:
        """Start asynchronous GitHub release check in background QThread."""
        if self.isRunning():
            return
        self._mode = "check"
        self.start()

    def start_download(self, download_url: str, filename: str = "") -> None:
        """Start asynchronous binary download of the release asset in background QThread."""
        if self.isRunning():
            return
        self._mode = "download"
        self._download_url = (download_url or "").strip()
        if filename:
            self._target_filename = filename
        else:
            url_Name = self._download_url.split("?")[0].rstrip("/").split("/")[-1]
            if url_Name.lower().endswith((".exe", ".zip")):
                self._target_filename = url_Name
            else:
                self._target_filename = "OmniPresence.exe"
        self.start()

    def run(self) -> None:
        if self._mode == "download":
            self._run_download()
        else:
            self._run_check()

    def _run_check(self) -> None:
        req = urllib.request.Request(
            self._api_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/vnd.github+json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                raw_body = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw_body)
        except urllib.error.HTTPError as exc:
            self.check_failed.emit(f"HTTP {exc.code}: {exc.reason}")
            return
        except Exception as exc:
            self.check_failed.emit(str(exc))
            return

        if not isinstance(payload, dict):
            self.check_failed.emit("Invalid GitHub release payload")
            return

        if payload.get("draft") or payload.get("prerelease"):
            return

        tag_name = str(payload.get("tag_name") or "").strip()
        if not tag_name:
            return

        if is_newer_version(tag_name, self._current_version):
            changelog = str(payload.get("body") or "").strip()
            download_url, asset_name = _select_release_asset(payload)
            if asset_name:
                self._target_filename = asset_name
            self.update_available.emit(tag_name, changelog, download_url)

    def _run_download(self) -> None:
        if not self._download_url:
            self.check_failed.emit("Download URL is empty")
            return

        req = urllib.request.Request(
            self._download_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/octet-stream, */*",
            },
            method="GET",
        )
        temp_dir = Path(tempfile.gettempdir()) / "OmniPresence_Update"
        try:
            temp_dir.mkdir(parents=True, exist_ok=True)
            dest_path = temp_dir / self._target_filename

            with urllib.request.urlopen(req, timeout=30.0) as resp:
                total_bytes = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                chunk_size = 64 * 1024
                self.download_progress.emit(0)

                with open(dest_path, "wb") as out_file:
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        out_file.write(chunk)
                        downloaded += len(chunk)
                        if total_bytes > 0:
                            pct = min(100, int((downloaded * 100) / total_bytes))
                            self.download_progress.emit(pct)

            self.download_progress.emit(100)
            self.download_finished.emit(str(dest_path))
        except Exception as exc:
            self.check_failed.emit(f"Download failed: {exc}")


def apply_update_and_restart(temp_installer_or_exe_path: str) -> bool:
    """Write temporary updater.bat to hot-swap OmniPresence.exe after process exit and restart."""
    source_path = Path(temp_installer_or_exe_path).resolve()
    if not source_path.is_file():
        return False

    # Determine currently running executable path (PyInstaller frozen binary or local dist binary)
    if getattr(sys, "frozen", False):
        current_exe = Path(sys.executable).resolve()
    else:
        dist_candidate = Path(__file__).resolve().parent.parent / "dist" / "OmniPresence" / "OmniPresence.exe"
        current_exe = dist_candidate if dist_candidate.exists() else Path(sys.executable).resolve()

    app_dir = current_exe.parent
    updater_bat = Path(tempfile.gettempdir()) / "omnipresence_updater.bat"

    is_setup_installer = "setup" in source_path.name.lower() and source_path.suffix.lower() == ".exe"
    is_zip_archive = source_path.suffix.lower() == ".zip"

    if is_setup_installer:
        replace_commands = (
            f'"{source_path}" /SILENT /NORESTART /CLOSEAPPLICATIONS\r\n'
            f'start "" "{current_exe}"\r\n'
        )
    elif is_zip_archive:
        replace_commands = (
            f'powershell -NoProfile -Command "Expand-Archive -Path \'{source_path}\' -DestinationPath \'{app_dir}\' -Force"\r\n'
            f'start "" "{current_exe}"\r\n'
        )
    else:
        replace_commands = (
            f'move /Y "{source_path}" "{current_exe}" >nul 2>&1\r\n'
            f'if exist "{source_path}" copy /Y "{source_path}" "{current_exe}" >nul 2>&1\r\n'
            f'start "" "{current_exe}"\r\n'
        )

    bat_content = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "timeout /t 1 /nobreak >nul\r\n"
        ":waitloop\r\n"
        'tasklist /FI "IMAGENAME eq OmniPresence.exe" 2>NUL | find /I /N "OmniPresence.exe">NUL\r\n'
        "if \"%ERRORLEVEL%\"==\"0\" (\r\n"
        "    timeout /t 1 /nobreak >nul\r\n"
        "    goto waitloop\r\n"
        ")\r\n"
        f"{replace_commands}"
        'del "%~f0"\r\n'
    )

    try:
        updater_bat.write_text(bat_content, encoding="utf-8")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        subprocess.Popen(
            ["cmd.exe", "/c", str(updater_bat)],
            cwd=str(tempfile.gettempdir()),
            creationflags=creation_flags,
            close_fds=True,
        )
        app = QApplication.instance()
        if app is not None:
            app.quit()
        return True
    except Exception:
        return False

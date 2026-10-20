"""OmniPresence bootstrapper: wires configuration, background workers, IPC, tray, and UI."""
from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from core.ipc_manager import IPCManager
from core.priority_engine import RuleBook, RuleError
from core.privacy_shield import PRIVACY_SHIELD
from core.process_watcher import ProcessWatcher
from core.settings_manager import SettingsManager
from core.state_manager import Settings, StateManager
from extractors.media import create_media_extractor
from ui import app_icon, resource_path, runtime_config_dir
from ui.main_window import MainWindow
from ui.tray_manager import TrayManager

STYLES_PATH = resource_path(Path("assets") / "styles" / "theme.qss")


def _sync_autostart(enabled: bool, script_path: Path) -> None:
    if sys.platform != "win32":
        return
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                if getattr(sys, "frozen", False):
                    cmd = f'"{Path(sys.executable).resolve()}" --minimized'
                else:
                    cmd = f'"{Path(sys.executable).resolve()}" "{script_path}" --minimized'
                winreg.SetValueEx(key, "OmniPresence", 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, "OmniPresence")
                except FileNotFoundError:
                    pass
    except OSError:
        pass


def _parse_smoke_arg(raw: str) -> float:
    text = (raw or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        parts = text.split(".")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return float(f"{parts[0]}.{parts[1]}")
        return 2.0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OmniPresence Discord Rich Presence Engine v2.4")
    parser.add_argument("--minimized", action="store_true", help="Start hidden in the system tray")
    parser.add_argument("--client-id", type=str, default="", help="Override Discord Application ID")
    parser.add_argument(
        "--smoke-test",
        type=_parse_smoke_arg,
        default=0.0,
        help="Run full engine and UI for N seconds and exit cleanly (0 = normal run)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    app = QApplication(sys.argv)
    app.setApplicationName("OmniPresence")
    app.setApplicationVersion("2.4")
    app.setOrganizationName("Raf446")
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(app_icon("disconnected"))

    if STYLES_PATH.is_file():
        app.setStyleSheet(STYLES_PATH.read_text(encoding="utf-8"))

    config_dir = runtime_config_dir()
    settings_path = config_dir / "settings.json"
    rules_path = config_dir / "rules.json"

    settings = SettingsManager.instance(settings_path).reload(settings_path)
    if args.client_id.strip():
        settings.use_custom_client_id = True
        settings.custom_client_id = args.client_id.strip()
        settings.client_id = settings.get_active_client_id()
        settings.discord_client_id = settings.get_active_client_id()

    script_file = Path(__file__).resolve()
    _sync_autostart(settings.auto_start, script_file)

    rule_error = ""
    try:
        rulebook = RuleBook.load(rules_path)
    except RuleError as exc:
        rule_error = str(exc)
        rulebook = RuleBook(())

    state = StateManager(settings, rules_path, rulebook, rule_error, privacy_shield=PRIVACY_SHIELD)
    state.settings_updated.connect(lambda s: _sync_autostart(s.auto_start, script_file))
    state.log("info", "boot", f"OmniPresence v2.4 initialized ({len(rulebook.rules)} rules loaded)")
    if rule_error:
        state.log("error", "rules", rule_error)

    watcher = ProcessWatcher(
        rulebook=rulebook,
        interval=settings.polling_interval,
        build_linger=settings.build_linger_seconds,
    )
    scrobbler = create_media_extractor(preferred_apps=settings.media_preferred_apps)
    ipc = IPCManager(client_id=settings.get_active_client_id())

    # Connect worker outputs -> StateManager
    watcher.scanned.connect(state.on_scan)
    watcher.message.connect(state.on_watcher_message)
    scrobbler.media_changed.connect(state.on_media)
    scrobbler.message.connect(state.on_media_message)
    ipc.status_changed.connect(state.on_connection)
    ipc.message.connect(state.on_ipc_message)
    ipc.presence_sent.connect(state.on_presence_sent)
    ipc.user_detected.connect(state.on_ipc_user)
    ipc.user_profile_updated.connect(state.on_ipc_profile)

    # Connect StateManager -> workers
    state.outgoing_changed.connect(ipc.submit)
    state.rules_changed.connect(watcher.on_rules_changed)
    state.client_id_changed.connect(ipc.set_client_id)

    window = MainWindow(state, tray_minimized=settings.tray_minimized)
    tray = TrayManager(settings.discord_app_name or "OmniPresence")

    tray.show_requested.connect(window.restore_from_tray)
    tray.pause_toggled.connect(state.set_paused)
    tray.reload_requested.connect(state.reload_rules)
    state.connection_changed.connect(tray.set_connection)
    state.paused_changed.connect(tray.set_paused)
    state.presence_changed.connect(tray.set_resolution)

    shutting_down = False

    def shutdown() -> None:
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        state.log("info", "shutdown", "stopping background workers and closing IPC pipe")
        window.prepare_quit()
        tray.hide()
        PRIVACY_SHIELD.stop()
        watcher.stop()
        scrobbler.stop()
        ipc.stop()
        app.quit()

    tray.quit_requested.connect(shutdown)
    app.aboutToQuit.connect(shutdown)
    signal.signal(signal.SIGINT, lambda *_: shutdown())

    tray.show()
    if not args.minimized:
        window.show()

    PRIVACY_SHIELD.start()
    watcher.start()
    scrobbler.start()
    ipc.start()

    if args.smoke_test > 0:
        QTimer.singleShot(int(args.smoke_test * 1000), shutdown)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())


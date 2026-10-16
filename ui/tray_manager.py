"""Windows system tray integration for background residency and quick actions."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from core.priority_engine import Resolution
from ui import app_icon


class TrayManager(QObject):
    show_requested = Signal()
    pause_toggled = Signal(bool)
    reload_requested = Signal()
    quit_requested = Signal()

    def __init__(self, app_name: str = "OmniPresence", parent=None) -> None:
        super().__init__(parent)
        self._app_name = app_name
        self._status = "disconnected"
        self._detail = "Starting"
        self._activity = "Standing by"
        self._paused = False

        self._tray = QSystemTrayIcon(app_icon("disconnected"), self)
        self._menu = QMenu()

        self._title_action = QAction(f"{app_name} — Disconnected", self._menu)
        self._title_action.setEnabled(False)
        self._menu.addAction(self._title_action)

        self._activity_action = QAction("Activity: Standing by", self._menu)
        self._activity_action.setEnabled(False)
        self._menu.addAction(self._activity_action)

        self._menu.addSeparator()

        self._open_action = QAction("Open OmniPresence", self._menu)
        self._open_action.triggered.connect(self.show_requested.emit)
        self._menu.addAction(self._open_action)

        self._pause_action = QAction("Pause Rich Presence", self._menu)
        self._pause_action.setCheckable(True)
        self._pause_action.triggered.connect(self._on_pause_triggered)
        self._menu.addAction(self._pause_action)

        self._reload_action = QAction("Reload Rules", self._menu)
        self._reload_action.triggered.connect(self.reload_requested.emit)
        self._menu.addAction(self._reload_action)

        self._menu.addSeparator()

        self._quit_action = QAction("Quit OmniPresence", self._menu)
        self._quit_action.triggered.connect(self.quit_requested.emit)
        self._menu.addAction(self._quit_action)

        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        self._update_tooltip()

    def show(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def notify(self, title: str, message: str) -> None:
        if self._tray.isVisible():
            self._tray.showMessage(title, message, app_icon(self._status), 2500)

    def set_connection(self, status: str, detail: str) -> None:
        self._status = status
        self._detail = detail or status.capitalize()
        self._tray.setIcon(app_icon(status))
        self._title_action.setText(f"{self._app_name} — {self._detail}")
        self._update_tooltip()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._pause_action.blockSignals(True)
        self._pause_action.setChecked(paused)
        self._pause_action.blockSignals(False)
        self._update_tooltip()

    def set_resolution(self, resolution: Resolution | None) -> None:
        if resolution is None or resolution.presence is None:
            self._activity = "No active context"
        else:
            details = resolution.presence.details or ""
            state = resolution.presence.state or ""
            self._activity = f"{details} ({state})" if state else details
        self._activity_action.setText(f"Activity: {self._activity[:48]}")
        self._update_tooltip()

    def _update_tooltip(self) -> None:
        state_flag = " [Paused]" if self._paused else ""
        self._tray.setToolTip(f"{self._app_name}{state_flag}\n{self._detail}\n{self._activity}")

    def _on_pause_triggered(self, checked: bool) -> None:
        self.pause_toggled.emit(checked)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_requested.emit()

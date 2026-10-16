"""Frameless main window with custom titlebar, Discord card preview, rules table, and activity log."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.state_manager import LogEntry, StateManager
from extractors.telemetry import TelemetrySample
from ui import DANGER, STATUS_COLORS, app_icon, mono_font, ui_font
from ui.components import DiscordPreview, QuickControls, RuleListWidget


class StatusDot(QWidget):
    """Solid status indicator dot inside the custom titlebar."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self._color = QColor(DANGER)

    def set_status(self, status: str) -> None:
        self._color = QColor(STATUS_COLORS.get(status, DANGER))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(QRectF(1, 1, 8, 8))


class CustomTitleBar(QFrame):
    minimize_clicked = Signal()
    maximize_clicked = Signal()
    close_clicked = Signal()

    def __init__(self, window: QMainWindow, app_name: str = "OmniPresence") -> None:
        super().__init__(window, objectName="TitleBar")
        self._window = window
        self._drag_offset: QPoint | None = None
        self.setFixedHeight(40)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 10, 0)
        layout.setSpacing(10)

        brand = QLabel(app_name, objectName="TitleBrand")
        brand.setFont(ui_font(13, QFont.Weight.DemiBold))
        layout.addWidget(brand)

        # Discord IPC connection badge
        pill = QFrame(objectName="StatusPill")
        pill_layout = QHBoxLayout(pill)
        pill_layout.setContentsMargins(8, 2, 8, 2)
        pill_layout.setSpacing(6)
        self._dot = StatusDot(pill)
        self._status_text = QLabel("IPC Disconnected", objectName="StatusText")
        self._status_text.setFont(mono_font(11))
        pill_layout.addWidget(self._dot)
        pill_layout.addWidget(self._status_text)
        layout.addWidget(pill)

        layout.addStretch(1)

        self._telemetry_label = QLabel("CPU --%  |  RAM --%", objectName="TitleTelemetry")
        self._telemetry_label.setFont(mono_font(11))
        layout.addWidget(self._telemetry_label)

        min_btn = QPushButton("\u2013", objectName="TitleButton")
        min_btn.setFont(mono_font(12, QFont.Weight.Bold))
        min_btn.setToolTip("Minimize")
        min_btn.clicked.connect(self.minimize_clicked.emit)
        layout.addWidget(min_btn)

        max_btn = QPushButton("\u25a1", objectName="TitleButton")
        max_btn.setFont(mono_font(11))
        max_btn.setToolTip("Maximize / Restore")
        max_btn.clicked.connect(self.maximize_clicked.emit)
        layout.addWidget(max_btn)

        close_btn = QPushButton("\u00d7", objectName="TitleCloseButton")
        close_btn.setFont(mono_font(14, QFont.Weight.Bold))
        close_btn.setToolTip("Hide to system tray (or close)")
        close_btn.clicked.connect(self.close_clicked.emit)
        layout.addWidget(close_btn)

    def set_connection(self, status: str, detail: str) -> None:
        self._dot.set_status(status)
        label = detail or f"IPC {status.capitalize()}"
        self._status_text.setText(label)

    def set_telemetry(self, sample: TelemetrySample) -> None:
        self._telemetry_label.setText(
            f"CPU {sample.cpu_percent:4.1f}%  |  RAM {sample.ram_used_gb:.1f}/{sample.ram_total_gb:.1f} GB ({sample.ram_percent:.0f}%)"
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._window.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
            self._drag_offset = event.globalPosition().toPoint() - self._window.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if not self._window.isMaximized():
                self._window.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximize_clicked.emit()


class ActivityLogPanel(QFrame):
    """Compact bottom console displaying real-time context triggers and IPC events."""

    def __init__(self, capacity: int = 500, parent=None) -> None:
        super().__init__(parent, objectName="LogPanel")
        self._capacity = capacity

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Activity & Trigger Console", objectName="SectionHeading")
        title.setFont(ui_font(12, QFont.Weight.DemiBold))
        header.addWidget(title)
        header.addStretch(1)

        clear_btn = QPushButton("Clear Log")
        clear_btn.setFont(ui_font(11))
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        header.addWidget(clear_btn)
        layout.addLayout(header)

        self._console = QPlainTextEdit(objectName="ActivityLogView")
        self._console.setReadOnly(True)
        self._console.setMaximumBlockCount(capacity)
        self._console.setFont(mono_font(11))
        self._console.setFixedHeight(130)
        clear_btn.clicked.connect(self._console.clear)
        layout.addWidget(self._console)

    def append_entry(self, entry: LogEntry) -> None:
        stamp = datetime.fromtimestamp(entry.timestamp).strftime("%H:%M:%S")
        tag = f"[{entry.level.upper():7}] [{entry.source:7}]"
        self._console.appendPlainText(f"{stamp} {tag} {entry.text}")
        self._console.moveCursor(QTextCursor.MoveOperation.End)


class MainWindow(QMainWindow):
    def __init__(self, state: StateManager, tray_minimized: bool = True) -> None:
        super().__init__()
        self._state = state
        self._tray_minimized = tray_minimized
        self._force_quit = False

        app_name = state.settings.discord_app_name or "OmniPresence"
        self.setWindowTitle(app_name)
        self.setWindowIcon(app_icon("disconnected"))
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        self.resize(1060, 720)
        self.setMinimumSize(900, 620)

        shell = QFrame(objectName="WindowShell")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        self._titlebar = CustomTitleBar(self, app_name)
        self._titlebar.minimize_clicked.connect(self.showMinimized)
        self._titlebar.maximize_clicked.connect(self._toggle_maximize)
        self._titlebar.close_clicked.connect(self.close)
        shell_layout.addWidget(self._titlebar)

        body = QWidget(objectName="WindowCanvas")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(14, 14, 14, 14)
        body_layout.setSpacing(14)

        # Left column: 1:1 Discord profile card mockup + priority tier strip
        left_col = QFrame(objectName="PanelCard")
        left_col.setFixedWidth(340)
        left_layout = QVBoxLayout(left_col)
        left_layout.setContentsMargins(12, 12, 12, 12)
        left_layout.setSpacing(10)

        preview_title = QLabel("Live Discord Card Preview", objectName="SectionHeading")
        preview_title.setFont(ui_font(13, QFont.Weight.DemiBold))
        left_layout.addWidget(preview_title)

        self._preview = DiscordPreview(app_name, left_col)
        left_layout.addWidget(self._preview, 1)
        body_layout.addWidget(left_col, 0)

        # Right column: Quick Controls + Rules Table + Activity Console
        right_col = QVBoxLayout()
        right_col.setSpacing(12)

        self._controls = QuickControls(state.settings.discord_client_id, body)
        self._controls.manual_submitted.connect(state.set_manual)
        self._controls.pause_toggled.connect(state.set_paused)
        self._controls.reload_requested.connect(state.reload_rules)
        self._controls.client_id_submitted.connect(state.set_client_id)
        right_col.addWidget(self._controls)

        self._rules = RuleListWidget(state.rulebook, body)
        self._rules.set_rulebook(state.rulebook, state.rule_error)
        self._rules.rule_toggled.connect(state.set_rule_enabled)
        right_col.addWidget(self._rules, 1)

        self._logs = ActivityLogPanel(state.settings.log_capacity, body)
        for existing in state.log_history:
            self._logs.append_entry(existing)
        right_col.addWidget(self._logs, 0)

        body_layout.addLayout(right_col, 1)
        shell_layout.addWidget(body, 1)
        self.setCentralWidget(shell)

        # Wire reactive StateManager signals
        state.presence_changed.connect(self._on_presence_changed)
        state.connection_changed.connect(self._on_connection_changed)
        state.telemetry_changed.connect(self._titlebar.set_telemetry)
        state.log_added.connect(self._logs.append_entry)
        state.rules_changed.connect(self._rules.set_rulebook)
        state.paused_changed.connect(self._on_paused_changed)
        state.manual_changed.connect(self._controls.set_manual_state)
        state.client_id_changed.connect(self._controls.set_client_id_text)

        self._on_connection_changed(*state.connection)
        if state.resolution is not None:
            self._on_presence_changed(state.resolution)

    def prepare_quit(self) -> None:
        self._force_quit = True

    def restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _on_presence_changed(self, resolution) -> None:
        self._preview.set_resolution(resolution)
        self._rules.set_resolution(resolution)

    def _on_connection_changed(self, status: str, detail: str) -> None:
        self.setWindowIcon(app_icon(status))
        self._titlebar.set_connection(status, detail)
        self._preview.set_connection(status, detail)

    def _on_paused_changed(self, paused: bool) -> None:
        self._controls.set_paused_state(paused)
        self._preview.set_paused(paused)

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._force_quit and self._tray_minimized:
            event.ignore()
            self.hide()
            self._state.log("info", "ui", "window hidden to system tray")
            return
        super().closeEvent(event)

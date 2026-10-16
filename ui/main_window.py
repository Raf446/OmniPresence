"""Frameless main window with custom titlebar, pulsating IPC glow dot, live CPU/RAM bar meters, Discord card preview, rules table, and activity log."""
from __future__ import annotations

import math
from datetime import datetime

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QRadialGradient,
    QTextCursor,
)
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
from ui import ACCENT, DANGER, STATUS_COLORS, SUCCESS, WARN, app_icon, mono_font, ui_font
from ui.components import DiscordPreview, QuickControls, RuleListWidget


class StatusDot(QWidget):
    """Pulsating glowing status indicator dot inside the custom titlebar."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self._status = "disconnected"
        self._color = QColor(STATUS_COLORS["disconnected"])
        self._phase = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.16) % (math.pi * 2)
        self.update()

    def set_status(self, status: str) -> None:
        self._status = status.lower()
        self._color = QColor(STATUS_COLORS.get(self._status, DANGER))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        center = QPointF(self.width() / 2, self.height() / 2)
        pulse = 0.5 + 0.5 * math.sin(self._phase)

        # Outer radial glow (pulsates strongly when connected or connecting)
        if self._status in ("connected", "connecting", "reconnecting"):
            radius = 5.5 + 2.3 * pulse
            alpha = int(95 + 95 * pulse)
        else:
            radius = 5.5
            alpha = 65

        glow = QRadialGradient(center, radius)
        c_inner = QColor(self._color)
        c_inner.setAlpha(alpha)
        glow.setColorAt(0.0, c_inner)
        c_outer = QColor(self._color)
        c_outer.setAlpha(0)
        glow.setColorAt(1.0, c_outer)
        painter.setBrush(glow)
        painter.drawEllipse(center, radius, radius)

        # Crisp core dot
        painter.setBrush(self._color)
        painter.drawEllipse(center, 3.6, 3.6)


class TelemetryBar(QWidget):
    """Compact visual micro-meter for CPU or RAM load with smooth fill and dynamic threshold color."""

    def __init__(self, label: str, base_color: str = ACCENT, parent=None) -> None:
        super().__init__(parent)
        self._label = label
        self._base_color = QColor(base_color)
        self._percent = 0.0
        self._display_percent = 0.0
        self._detail = "--%"
        self.setFixedSize(138, 22)

    def set_metric(self, percent: float, detail: str) -> None:
        self._percent = min(max(percent, 0.0), 100.0)
        self._display_percent += (self._percent - self._display_percent) * 0.55
        self._detail = detail
        self.setToolTip(f"{self._label}: {detail}")
        self.update()

    def _active_color(self) -> QColor:
        if self._percent >= 85.0:
            return QColor(DANGER)
        if self._percent >= 65.0:
            return QColor(WARN)
        return self._base_color

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Label + value on top row
        painter.setFont(mono_font(10, QFont.Weight.DemiBold))
        painter.setPen(QColor("#94a0b8"))
        painter.drawText(QRectF(0, 0, 34, 13), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        painter.setPen(QColor("#eef2f8"))
        painter.drawText(
            QRectF(34, 0, self.width() - 34, 13),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            self._detail,
        )

        # Progress bar track on bottom row
        bar_rect = QRectF(0, 15, self.width(), 5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 22))
        painter.drawRoundedRect(bar_rect, 2.5, 2.5)

        fill_w = (self.width() * (self._display_percent / 100.0))
        if fill_w > 1.0:
            col = self._active_color()
            grad = QLinearGradient(0, 0, fill_w, 0)
            grad.setColorAt(0.0, col.darker(115))
            grad.setColorAt(1.0, col.lighter(120))
            painter.setBrush(grad)
            painter.drawRoundedRect(QRectF(0, 15, max(4.0, fill_w), 5), 2.5, 2.5)


class CustomTitleBar(QFrame):
    minimize_clicked = Signal()
    maximize_clicked = Signal()
    close_clicked = Signal()

    def __init__(self, window: QMainWindow, app_name: str = "OmniPresence") -> None:
        super().__init__(window, objectName="TitleBar")
        self._window = window
        self._drag_offset: QPoint | None = None
        self.setFixedHeight(44)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 10, 0)
        layout.setSpacing(10)

        brand = QLabel(app_name, objectName="TitleBrand")
        brand.setFont(ui_font(13, QFont.Weight.Bold))
        layout.addWidget(brand)

        ver = QLabel("v1.2.0", objectName="TitleVersionBadge")
        ver.setFont(mono_font(10, QFont.Weight.DemiBold))
        layout.addWidget(ver)

        # Discord IPC connection badge with pulsating status dot
        self._pill = QFrame(objectName="StatusPill")
        self._pill.setProperty("status", "disconnected")
        pill_layout = QHBoxLayout(self._pill)
        pill_layout.setContentsMargins(8, 2, 10, 2)
        pill_layout.setSpacing(6)
        self._dot = StatusDot(self._pill)
        self._status_text = QLabel("IPC Disconnected", objectName="StatusText")
        self._status_text.setFont(mono_font(11, QFont.Weight.DemiBold))
        pill_layout.addWidget(self._dot)
        pill_layout.addWidget(self._status_text)
        layout.addWidget(self._pill)

        layout.addStretch(1)

        # Live Telemetry micro-stats cluster (CPU & RAM bar meters)
        telemetry_cluster = QFrame(objectName="TelemetryCluster")
        tc_layout = QHBoxLayout(telemetry_cluster)
        tc_layout.setContentsMargins(10, 3, 10, 3)
        tc_layout.setSpacing(14)
        self._cpu_bar = TelemetryBar("CPU", "#2979ff", telemetry_cluster)
        self._ram_bar = TelemetryBar("RAM", "#b388ff", telemetry_cluster)
        tc_layout.addWidget(self._cpu_bar)
        tc_layout.addWidget(self._ram_bar)
        layout.addWidget(telemetry_cluster)

        min_btn = QPushButton("\u2013", objectName="TitleButton")
        min_btn.setFont(mono_font(12, QFont.Weight.Bold))
        min_btn.setToolTip("Minimize")
        min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        min_btn.clicked.connect(self.minimize_clicked.emit)
        layout.addWidget(min_btn)

        max_btn = QPushButton("\u25a1", objectName="TitleButton")
        max_btn.setFont(mono_font(11))
        max_btn.setToolTip("Maximize / Restore")
        max_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        max_btn.clicked.connect(self.maximize_clicked.emit)
        layout.addWidget(max_btn)

        close_btn = QPushButton("\u00d7", objectName="TitleCloseButton")
        close_btn.setFont(mono_font(14, QFont.Weight.Bold))
        close_btn.setToolTip("Hide to system tray (or close)")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.close_clicked.emit)
        layout.addWidget(close_btn)

    def set_connection(self, status: str, detail: str) -> None:
        norm = status.lower()
        self._dot.set_status(norm)
        self._pill.setProperty("status", norm)
        self._pill.style().unpolish(self._pill)
        self._pill.style().polish(self._pill)
        label = detail or f"IPC {status.capitalize()}"
        self._status_text.setText(label)

    def set_telemetry(self, sample: TelemetrySample) -> None:
        self._cpu_bar.set_metric(sample.cpu_percent, f"{sample.cpu_percent:4.1f}%")
        self._ram_bar.set_metric(
            sample.ram_percent,
            f"{sample.ram_used_gb:.1f}/{sample.ram_total_gb:.0f}G ({sample.ram_percent:.0f}%)",
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
        self.resize(1100, 740)
        self.setMinimumSize(940, 640)

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
        left_col.setFixedWidth(352)
        left_layout = QVBoxLayout(left_col)
        left_layout.setContentsMargins(12, 12, 12, 12)
        left_layout.setSpacing(10)

        preview_title = QLabel("Live Discord Card Preview", objectName="SectionHeading")
        preview_title.setFont(ui_font(13, QFont.Weight.DemiBold))
        left_layout.addWidget(preview_title)

        display_name, handle = state.profile
        self._preview = DiscordPreview(app_name, display_name, handle, left_col)
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
        state.profile_changed.connect(self._preview.set_profile)

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

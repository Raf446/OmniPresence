"""Frameless Obsidian Glass main window (v2.3.0) with Collapsible Sidebar + 5-page AnimatedStackedWidget."""
from __future__ import annotations

import html
import math
from datetime import datetime

from PySide6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.analytics_manager import (
    ANALYTICS_MANAGER,
    CATEGORIES,
    CATEGORY_COLORS,
    CATEGORY_ICONS,
    DailyAnalyticsSnapshot,
    format_analytics_duration,
)
from core.priority_engine import Resolution, Tier
from core.settings_manager import DEFAULT_CLIENT_ID
from core.updater import CURRENT_VERSION, UpdateCheckerThread, apply_update_and_restart
from core.state_manager import LogEntry, StateManager
from extractors.celestial_sync import CELESTIAL_SYNC
from extractors.telemetry import TelemetrySample
from ui import (
    ACCENT,
    DANGER,
    STATUS_COLORS,
    SUCCESS,
    TIER_COLORS,
    TIER_TITLES,
    WARN,
    app_icon,
    mono_font,
    ui_font,
)
from ui.components import (
    AnimatedStackedWidget,
    CustomRPCPanel,
    DiscordPreview,
    QuickControls,
    RuleListWidget,
    SettingsCard,
    ToggleSwitch,
)


class StatusDot(QWidget):
    """Pulsating glowing status indicator dot inside the sidebar footer and tier matrix."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(14, 14)
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

    def set_custom_color(self, hex_color: str, active: bool = True) -> None:
        self._status = "connected" if active else "disconnected"
        self._color = QColor(hex_color)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        center = QPointF(self.width() / 2, self.height() / 2)
        pulse = 0.5 + 0.5 * math.sin(self._phase)

        if self._status in ("connected", "connecting", "reconnecting"):
            radius = 5.0 + 2.0 * pulse
            alpha = int(95 + 95 * pulse)
        else:
            radius = 4.5
            alpha = 55

        glow = QRadialGradient(center, radius)
        c_inner = QColor(self._color)
        c_inner.setAlpha(alpha)
        glow.setColorAt(0.0, c_inner)
        c_outer = QColor(self._color)
        c_outer.setAlpha(0)
        glow.setColorAt(1.0, c_outer)
        painter.setBrush(glow)
        painter.drawEllipse(center, radius, radius)

        painter.setBrush(self._color)
        painter.drawEllipse(center, 3.3, 3.3)


class TelemetryBar(QWidget):
    """Compact visual micro-meter for CPU or RAM load with slim glowing progress bar (5px) beside metric text."""

    def __init__(self, label: str, base_color: str = ACCENT, parent=None) -> None:
        super().__init__(parent)
        self._label = label
        self._base_color = QColor(base_color)
        self._percent = 0.0
        self._display_percent = 0.0
        self._detail = "--%"
        self.setFixedSize(156, 22)

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

        painter.setFont(mono_font(10, QFont.Weight.Bold))
        painter.setPen(QColor("#8B949E"))
        painter.drawText(QRectF(0, 0, 30, 22), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        bar_x = 32.0
        bar_w = 54.0
        bar_y = 8.5
        bar_h = 5.0
        bar_rect = QRectF(bar_x, bar_y, bar_w, bar_h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 24))
        painter.drawRoundedRect(bar_rect, 2.5, 2.5)

        fill_w = bar_w * (self._display_percent / 100.0)
        if fill_w > 1.0:
            col = self._active_color()
            grad = QLinearGradient(bar_x, 0, bar_x + fill_w, 0)
            grad.setColorAt(0.0, col)
            grad.setColorAt(1.0, QColor("#A855F7") if self._label == "CPU" else col.lighter(125))
            painter.setBrush(grad)
            painter.drawRoundedRect(QRectF(bar_x, bar_y, max(4.0, fill_w), bar_h), 2.5, 2.5)

        painter.setFont(mono_font(10, QFont.Weight.DemiBold))
        painter.setPen(QColor("#F8FAFC"))
        painter.drawText(
            QRectF(bar_x + bar_w + 6, 0, self.width() - (bar_x + bar_w + 6), 22),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._detail,
        )


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
        layout.setContentsMargins(16, 0, 10, 0)
        layout.setSpacing(10)

        brand = QLabel(app_name, objectName="TitleBrand")
        brand.setFont(ui_font(13, QFont.Weight.Bold))
        layout.addWidget(brand)

        ver = QLabel("v2.3.0", objectName="TitleVersionBadge")
        ver.setFont(mono_font(10, QFont.Weight.DemiBold))
        layout.addWidget(ver)

        layout.addStretch(1)

        telemetry_cluster = QFrame(objectName="TelemetryCluster")
        tc_layout = QHBoxLayout(telemetry_cluster)
        tc_layout.setContentsMargins(10, 3, 10, 3)
        tc_layout.setSpacing(12)
        self._cpu_bar = TelemetryBar("CPU", "#00E5FF", telemetry_cluster)
        self._ram_bar = TelemetryBar("RAM", "#A855F7", telemetry_cluster)
        tc_layout.addWidget(self._cpu_bar)
        tc_layout.addWidget(self._ram_bar)
        layout.addWidget(telemetry_cluster)

        min_btn = QPushButton("\u2014", objectName="TitleButton")
        min_btn.setFont(mono_font(11, QFont.Weight.Bold))
        min_btn.setToolTip("Minimize")
        min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        min_btn.clicked.connect(self.minimize_clicked.emit)
        layout.addWidget(min_btn)

        max_btn = QPushButton("\u25a2", objectName="TitleButton")
        max_btn.setFont(mono_font(11))
        max_btn.setToolTip("Maximize / Restore")
        max_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        max_btn.clicked.connect(self.maximize_clicked.emit)
        layout.addWidget(max_btn)

        close_btn = QPushButton("\u2715", objectName="TitleCloseButton")
        close_btn.setFont(mono_font(11, QFont.Weight.Bold))
        close_btn.setToolTip("Hide to system tray (or close)")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.close_clicked.emit)
        layout.addWidget(close_btn)

    def set_connection(self, status: str, detail: str) -> None:
        # Connection status is centralized in SidebarFooter to prevent topbar visual redundancy
        pass

    def set_telemetry(self, sample: TelemetrySample) -> None:
        self._cpu_bar.set_metric(sample.cpu_percent, f"{sample.cpu_percent:4.1f}%")
        self._ram_bar.set_metric(
            sample.ram_percent,
            f"{sample.ram_used_gb:.1f}/{sample.ram_total_gb:.0f}G",
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


class TierStatusGrid(QFrame):
    """Dynamic Priority Tier Matrix (2 columns x 3 rows) with anti-clipping and pulsating status dots."""

    MATRIX_LABELS: dict[int, str] = {
        1: "P1 Manual / Shield",
        2: "P2 Compiler Active",
        3: "P3 Gaming Mode",
        4: "P4 IDE / Git Control",
        5: "P5 Media Playing",
        6: "P6 Idle / Celestial",
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent, objectName="HudCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("Dynamic Priority Tier Matrix", objectName="SectionHeading")
        title.setFont(ui_font(12, QFont.Weight.Bold))
        header.addWidget(title)
        header.addStretch(1)

        self._celestial_badge = QLabel("\U0001f319 Celestial Sync", objectName="CardPillBadge")
        self._celestial_badge.setFont(ui_font(10, QFont.Weight.DemiBold))
        header.addWidget(self._celestial_badge)

        self._shield_badge = QLabel("\U0001f6e1\ufe0f Shield Standby", objectName="CardPillBadge")
        self._shield_badge.setFont(ui_font(10, QFont.Weight.DemiBold))
        header.addWidget(self._shield_badge)

        root.addLayout(header)

        # 2 Columns x 3 Rows Grid Layout:
        # Row 0: P1 Manual / Shield  | P2 Compiler Active
        # Row 1: P3 Gaming Mode      | P4 IDE / Git Control
        # Row 2: P5 Media Playing    | P6 Idle / Celestial
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        self._tier_pills: dict[int, tuple[QFrame, StatusDot, QLabel]] = {}
        for idx, tier_num in enumerate(range(1, 7)):
            card = QFrame(self)
            card.setObjectName("DiscordBox")
            card.setMinimumWidth(150)
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            c_layout = QHBoxLayout(card)
            c_layout.setContentsMargins(12, 8, 12, 8)
            c_layout.setSpacing(8)

            dot = StatusDot(card)
            dot.set_custom_color("#64748B", active=False)
            c_layout.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)

            name_lbl = QLabel(self.MATRIX_LABELS[tier_num])
            name_lbl.setFont(ui_font(11, QFont.Weight.DemiBold))
            name_lbl.setMinimumWidth(115)
            name_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            c_layout.addWidget(name_lbl, 1)

            state_lbl = QLabel("IDLE", objectName="SectionCaption")
            state_lbl.setFont(mono_font(10, QFont.Weight.Bold))
            c_layout.addWidget(state_lbl, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            self._tier_pills[tier_num] = (card, dot, state_lbl)
            grid.addWidget(card, idx // 2, idx % 2)

        root.addLayout(grid)
        self.refresh_celestial()

    def refresh_celestial(self) -> None:
        snap = CELESTIAL_SYNC.snapshot(allow_network=False)
        self._celestial_badge.setText(f"{snap.ambient_vibe} ({snap.utc_offset_str})")

    def set_shield_state(self, engaged: bool, enabled: bool) -> None:
        if not enabled:
            self._shield_badge.setText("\U0001f6e1\ufe0f Shield OFF")
        elif engaged:
            self._shield_badge.setText("\U0001f6e1\ufe0f STEALTH LOCKED")
        else:
            self._shield_badge.setText("\U0001f6e1\ufe0f Shield Armed")

    def set_resolution(self, resolution: Resolution | None) -> None:
        self.refresh_celestial()
        active_tier = int(resolution.tier) if (resolution and resolution.tier is not None) else None
        live_set = {int(t) for t in resolution.live_tiers} if resolution else set()

        for tier_num, (card, dot, state_lbl) in self._tier_pills.items():
            col_hex = TIER_COLORS[tier_num]
            if tier_num == active_tier:
                dot.set_custom_color("#10B981" if tier_num != 1 else col_hex, active=True)
                state_lbl.setText("[ACTIVE]")
                state_lbl.setStyleSheet(
                    f"background-color: {col_hex}26; color: {col_hex}; border: 1px solid {col_hex}80; "
                    "border-radius: 5px; padding: 1px 6px;"
                )
                card.setStyleSheet(
                    f"QFrame#DiscordBox {{ background-color: {col_hex}18; border: 1px solid {col_hex}88; border-radius: 12px; }}"
                )
            elif tier_num in live_set:
                dot.set_custom_color(col_hex, active=True)
                state_lbl.setText("READY")
                state_lbl.setStyleSheet("color: #E2E8F0; padding: 1px 4px;")
                card.setStyleSheet("")
            else:
                dot.set_custom_color("#64748B", active=False)
                state_lbl.setText("IDLE")
                state_lbl.setStyleSheet("color: rgba(148, 163, 184, 0.55); padding: 1px 4px;")
                card.setStyleSheet("")


class ActivityLogPanel(QFrame):
    """Modern Card-Stream Console displaying real-time context triggers, contextual icons, and IPC events."""

    SOURCE_ICONS: dict[str, tuple[str, str]] = {
        "media": ("\U0001f3b5 Spotify", "#A855F7"),
        "spotify": ("\U0001f3b5 Spotify", "#10B981"),
        "youtube": ("\u25b6 YouTube", "#EF4444"),
        "github": ("\U0001f4bb GitHub", "#79C0FF"),
        "ipc": ("\U0001f50c IPC", "#00E5FF"),
        "gateway": ("\U0001f310 Gateway", "#34D399"),
        "compiler": ("\u2699\ufe0f Compiler", "#F59E0B"),
        "shield": ("\U0001f6e1\ufe0f Shield", "#EF4444"),
        "mood": ("\U0001f3a8 Mood", "#A855F7"),
        "celestial": ("\U0001f319 Celestial", "#38BDF8"),
        "rules": ("\u2630 Rules", "#79C0FF"),
        "boot": ("\u2728 Boot", "#10B981"),
    }

    def __init__(self, capacity: int = 500, parent=None) -> None:
        super().__init__(parent, objectName="LogPanel")
        self._capacity = capacity
        self._line_count = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Activity & Trigger Console", objectName="SectionHeading")
        title.setFont(ui_font(12, QFont.Weight.Bold))
        header.addWidget(title)
        header.addStretch(1)

        clear_btn = QPushButton("Clear Log")
        clear_btn.setFont(ui_font(11, QFont.Weight.DemiBold))
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        header.addWidget(clear_btn)
        layout.addLayout(header)

        self._console = QTextEdit(objectName="ActivityLogView")
        self._console.setReadOnly(True)
        self._console.setFont(mono_font(11))
        self._console.setMinimumHeight(135)
        clear_btn.clicked.connect(self._on_clear)
        layout.addWidget(self._console, 1)

    def _on_clear(self) -> None:
        self._console.clear()
        self._line_count = 0

    def append_entry(self, entry: LogEntry) -> None:
        if self._line_count >= self._capacity:
            self._console.clear()
            self._line_count = 0
        self._line_count += 1

        stamp = datetime.fromtimestamp(entry.timestamp).strftime("%H:%M:%S")
        src_lower = entry.source.lower()
        tag_label, tag_col = self.SOURCE_ICONS.get(src_lower, (f"\u26a1 {entry.source}", "#79C0FF"))

        lvl = entry.level.lower()
        if lvl == "error":
            msg_col = "#FCA5A5"
        elif lvl == "warn":
            msg_col = "#FDE68A"
        elif lvl == "trigger":
            msg_col = "#E2E8F0"
            tag_col = "#00E5FF"
        else:
            msg_col = "#C9D1D9"

        safe_text = html.escape(entry.text)
        safe_tag = html.escape(f"[{tag_label}]")
        line_html = (
            f'<div style="margin-bottom: 3px;">'
            f'<span style="color: #6E7681;">{stamp}</span> '
            f'<span style="color: {tag_col}; font-weight: 600;">{safe_tag}</span> '
            f'<span style="color: {msg_col};">{safe_text}</span>'
            f"</div>"
        )
        self._console.append(line_html)
        self._console.moveCursor(QTextCursor.MoveOperation.End)


class SettingsPage(QScrollArea):
    """Page 4 (Settings): Discord Gateway Configuration + 3 v2.3.0 Innovations + General Preferences."""

    def __init__(self, state: StateManager, parent=None) -> None:
        super().__init__(parent, objectName="SettingsScrollArea")
        self._state = state
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget(self, objectName="SettingsScrollContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 2, 10, 12)
        layout.setSpacing(14)

        s = state.settings

        # Card 0: Discord Gateway Configuration (Central Gateway Architecture)
        self._gateway_card = SettingsCard(
            "\U0001f310 Discord Gateway Configuration",
            "Connects automatically via the Official OmniPresence Application ID Gateway out-of-the-box, or switch to a custom Developer Portal Application ID.",
            has_toggle=False,
            checked=True,
            badge_text="Central Gateway",
            parent=content,
        )

        self._gateway_status_badge = QLabel("", objectName="GatewayBadge")
        self._gateway_status_badge.setFont(mono_font(11, QFont.Weight.DemiBold))
        self._gateway_card.add_row(
            "Active Gateway Mode",
            self._gateway_status_badge,
            f"Official Default Application ID: {DEFAULT_CLIENT_ID}",
        )

        self._use_custom_id_switch = ToggleSwitch(checked=getattr(s, "use_custom_client_id", False))
        self._use_custom_id_switch.toggled.connect(self._on_custom_gateway_toggled)
        self._gateway_card.add_row(
            "Use Custom Application ID",
            self._use_custom_id_switch,
            "OFF = Official OmniPresence Gateway (Recommended) • ON = Custom Discord App ID",
        )

        id_box = QWidget()
        id_layout = QHBoxLayout(id_box)
        id_layout.setContentsMargins(0, 0, 0, 0)
        id_layout.setSpacing(8)
        initial_display_id = (
            s.custom_client_id.strip()
            if (getattr(s, "use_custom_client_id", False) and getattr(s, "custom_client_id", "").strip())
            else DEFAULT_CLIENT_ID
        )
        self._client_id_edit = QLineEdit(initial_display_id)
        self._client_id_edit.setFont(mono_font(11))
        self._client_id_edit.setMinimumWidth(220)
        self._client_id_edit.setPlaceholderText("Enter 18-19 digit Custom Application ID")
        self._apply_gateway_btn = QPushButton("Apply & Reconnect", objectName="PrimaryButton")
        self._apply_gateway_btn.setFont(ui_font(11, QFont.Weight.Bold))
        self._apply_gateway_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._apply_gateway_btn.clicked.connect(self._on_apply_gateway_clicked)
        id_layout.addWidget(self._client_id_edit, 1)
        id_layout.addWidget(self._apply_gateway_btn)
        self._gateway_card.add_row("Application ID", id_box)

        self._refresh_gateway_ui(getattr(s, "use_custom_client_id", False), getattr(s, "custom_client_id", ""))
        layout.addWidget(self._gateway_card)

        # Card 1: Anti-AFK Privacy Shield & Biometric Lock
        self._shield_card = SettingsCard(
            "\U0001f6e1\ufe0f Anti-AFK Privacy Shield & Biometric Lock",
            "Hooks Win32 WTSRegisterSessionNotification (Win+L) and GetLastInputInfo() to cloak your Discord presence while away.",
            has_toggle=True,
            checked=s.privacy_shield_enabled,
            badge_text="Tier P1 Fast-Lane",
            parent=content,
        )
        self._shield_card.toggled.connect(lambda v: self._state.update_settings(privacy_shield_enabled=v))

        self._lock_hook_switch = ToggleSwitch(checked=s.privacy_lock_hook)
        self._lock_hook_switch.toggled.connect(lambda v: self._state.update_settings(privacy_lock_hook=v))
        self._shield_card.add_row(
            "Win32 Session Lock Hook (Win + L)",
            self._lock_hook_switch,
            "Instantly switch to Stealth Mode when Windows workstation locks",
        )

        self._inactivity_spin = QSpinBox()
        self._inactivity_spin.setFont(mono_font(11))
        self._inactivity_spin.setRange(0, 240)
        self._inactivity_spin.setValue(s.privacy_inactivity_minutes)
        self._inactivity_spin.setSuffix(" min")
        self._inactivity_spin.setToolTip("0 = disable inactivity auto-stealth")
        self._inactivity_spin.valueChanged.connect(
            lambda val: self._state.update_settings(privacy_inactivity_minutes=int(val))
        )
        self._shield_card.add_row(
            "Inactivity Auto-Stealth Timeout",
            self._inactivity_spin,
            "Engage Stealth Mode after N minutes without keyboard/mouse input (0 = off)",
        )

        self._stealth_details_edit = QLineEdit(s.privacy_stealth_details)
        self._stealth_details_edit.setFont(ui_font(11))
        self._stealth_details_edit.setMinimumWidth(280)
        self._stealth_details_edit.editingFinished.connect(
            lambda: self._state.update_settings(privacy_stealth_details=self._stealth_details_edit.text().strip())
        )
        self._shield_card.add_row("Stealth Headline (Details)", self._stealth_details_edit)

        sim_btn = QPushButton("Simulate Session Lock / Unlock")
        sim_btn.setFont(ui_font(11, QFont.Weight.DemiBold))
        sim_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sim_btn.clicked.connect(
            lambda: self._state.privacy_shield.simulate_session_lock(not self._state.privacy_shield.is_engaged)
        )
        self._shield_card.add_row(
            "Live Stealth Verification",
            sim_btn,
            "Toggle simulated workstation lock to test P1 Stealth override immediately",
        )
        layout.addWidget(self._shield_card)

        # Card 2: Adaptive Mood Engine
        self._mood_card = SettingsCard(
            "\U0001f3a8 Adaptive Mood Engine",
            "Quantizes 48x48 album covers and YouTube thumbnails into HSL dominant palettes (Vibrant Neon, Tokyo Night, Warm Lo-Fi, Monochrome).",
            has_toggle=True,
            checked=s.mood_engine_enabled,
            badge_text="QThreadPool HSL",
            parent=content,
        )
        self._mood_card.toggled.connect(lambda v: self._state.update_settings(mood_engine_enabled=v))

        self._mood_badge_switch = ToggleSwitch(checked=s.mood_apply_to_small_badge)
        self._mood_badge_switch.toggled.connect(lambda v: self._state.update_settings(mood_apply_to_small_badge=v))
        self._mood_card.add_row(
            "Dynamic Mood Badge & Tooltip Injection",
            self._mood_badge_switch,
            "Inject {mood_badge} and {mood_name} into active Media & YouTube Rich Presence",
        )

        chips_row = QWidget()
        chips_layout = QHBoxLayout(chips_row)
        chips_layout.setContentsMargins(0, 0, 0, 0)
        chips_layout.setSpacing(6)
        for label, hex_col in (
            ("Vibrant Neon", "#00E5FF"),
            ("Tokyo Night", "#7C4DFF"),
            ("Warm Lo-Fi", "#FF8A65"),
            ("Monochrome", "#94A3B8"),
        ):
            chip = QLabel(label)
            chip.setFont(mono_font(10, QFont.Weight.DemiBold))
            chip.setStyleSheet(
                f"background-color: {hex_col}22; color: {hex_col}; border: 1px solid {hex_col}66; "
                "border-radius: 6px; padding: 2px 8px;"
            )
            chips_layout.addWidget(chip)
        self._mood_card.add_row("Supported Mood Classifications", chips_row)
        layout.addWidget(self._mood_card)

        # Card 3: Ambient World & Celestial Sync
        self._celestial_card = SettingsCard(
            "\U0001f319 Ambient World & Celestial Sync",
            "Computes local solar ephemeris (Dawn, Day, Golden Hour, Dusk, Night), lunar synodic phase, and Open-Meteo ECMWF/NOAA weather.",
            has_toggle=True,
            checked=s.celestial_sync_enabled,
            badge_text="Solar / Lunar / Weather",
            parent=content,
        )
        self._celestial_card.toggled.connect(lambda v: self._state.update_settings(celestial_sync_enabled=v))

        self._celestial_append_switch = ToggleSwitch(checked=s.celestial_append_to_state)
        self._celestial_append_switch.toggled.connect(
            lambda v: self._state.update_settings(celestial_append_to_state=v)
        )
        self._celestial_card.add_row(
            "Embed {ambient_vibe} into Presence Tooltip / Idle State",
            self._celestial_append_switch,
            "Automatically decorate small badge text and AFK/Standby presence with live sky phase",
        )

        self._weather_switch = ToggleSwitch(checked=s.celestial_weather_enabled)
        self._weather_switch.toggled.connect(lambda v: self._state.update_settings(celestial_weather_enabled=v))
        self._celestial_card.add_row(
            "Open-Meteo Live Weather Sync (ECMWF/NOAA â€¢ No API Key)",
            self._weather_switch,
            "Fetch local temperature {temp_c}Â°C and WMO sky condition (cached 40 minutes)",
        )

        # Tiered Location Selector (Tier 1: Windows GPS Sensor vs Tier 2: Manual City Search)
        loc_group_frame = QFrame(objectName="SubCardFrame")
        loc_group_layout = QVBoxLayout(loc_group_frame)
        loc_group_layout.setContentsMargins(10, 8, 10, 8)
        loc_group_layout.setSpacing(8)

        self._loc_btn_group = QButtonGroup(self)
        self._radio_gps = QRadioButton("Gunakan GPS / Windows Location Sensor")
        self._radio_gps.setFont(ui_font(11, QFont.Weight.DemiBold))
        self._radio_manual = QRadioButton("Atur Lokasi Manual")
        self._radio_manual.setFont(ui_font(11, QFont.Weight.DemiBold))
        self._loc_btn_group.addButton(self._radio_gps, 1)
        self._loc_btn_group.addButton(self._radio_manual, 2)

        is_manual = getattr(s, "location_mode", "gps").strip().lower() == "manual"
        self._radio_manual.setChecked(is_manual)
        self._radio_gps.setChecked(not is_manual)

        # Option 1 Row: GPS Radio + [ Detect Now ] + Coordinates Indicator
        gps_row = QHBoxLayout()
        gps_row.setSpacing(8)
        gps_row.addWidget(self._radio_gps)
        self._detect_gps_btn = QPushButton("Detect Now")
        self._detect_gps_btn.setFont(ui_font(10, QFont.Weight.DemiBold))
        self._detect_gps_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._detect_gps_btn.clicked.connect(self._on_detect_gps_clicked)
        gps_row.addWidget(self._detect_gps_btn)
        self._gps_coords_lbl = QLabel(
            f"Lat: {s.celestial_latitude:.4f}, Lon: {s.celestial_longitude:.4f}",
            objectName="SectionCaption",
        )
        self._gps_coords_lbl.setFont(mono_font(10))
        gps_row.addWidget(self._gps_coords_lbl, 1)
        loc_group_layout.addLayout(gps_row)

        # Option 2 Row: Manual Radio + City QLineEdit + [ Cari / Set ]
        manual_row = QHBoxLayout()
        manual_row.setSpacing(8)
        manual_row.addWidget(self._radio_manual)
        self._manual_city_edit = QLineEdit(getattr(s, "manual_location_name", ""))
        self._manual_city_edit.setFont(ui_font(11))
        self._manual_city_edit.setPlaceholderText("Ketik kota/kecamatan (misal: Batujajar, Bandung, Jakarta)")
        self._manual_city_edit.returnPressed.connect(self._on_search_city_clicked)
        self._search_city_btn = QPushButton("Cari / Set", objectName="PrimaryButton")
        self._search_city_btn.setFont(ui_font(11, QFont.Weight.DemiBold))
        self._search_city_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._search_city_btn.clicked.connect(self._on_search_city_clicked)
        manual_row.addWidget(self._manual_city_edit, 1)
        manual_row.addWidget(self._search_city_btn)
        loc_group_layout.addLayout(manual_row)

        self._radio_gps.toggled.connect(self._on_location_mode_toggled)
        self._celestial_card.add_block(loc_group_frame)

        coord_box = QWidget()
        coord_layout = QHBoxLayout(coord_box)
        coord_layout.setContentsMargins(0, 0, 0, 0)
        coord_layout.setSpacing(6)
        self._lat_spin = QDoubleSpinBox()
        self._lat_spin.setRange(-90.0, 90.0)
        self._lat_spin.setDecimals(4)
        self._lat_spin.setValue(s.manual_lat if is_manual else s.celestial_latitude)
        self._lat_spin.setPrefix("Lat: ")
        self._lon_spin = QDoubleSpinBox()
        self._lon_spin.setRange(-180.0, 180.0)
        self._lon_spin.setDecimals(4)
        self._lon_spin.setValue(s.manual_lon if is_manual else s.celestial_longitude)
        self._lon_spin.setPrefix("Lon: ")
        self._lat_spin.valueChanged.connect(
            lambda v: self._state.update_settings(celestial_latitude=float(v), manual_lat=float(v))
        )
        self._lon_spin.valueChanged.connect(
            lambda v: self._state.update_settings(celestial_longitude=float(v), manual_lon=float(v))
        )
        coord_layout.addWidget(self._lat_spin)
        coord_layout.addWidget(self._lon_spin)
        self._celestial_card.add_row("Active Coordinates (Lat / Lon)", coord_box)

        # Privacy Toggle: Show/Hide City Name in Discord Presence
        self._show_city_switch = ToggleSwitch(checked=getattr(s, "privacy_show_city", False))
        self._show_city_switch.toggled.connect(
            lambda v: self._state.update_settings(privacy_show_city=bool(v))
        )
        self._celestial_card.add_row(
            "Tampilkan Nama Kota/Wilayah di Status Discord (Matikan untuk Privasi)",
            self._show_city_switch,
            "Jika OFF (Privasi Aktif), status IDLE hanya menampilkan suhu & cuaca (contoh: 23Â°C â€¢ Partly Cloudy)",
        )

        # Custom Idle Presence Icon Block
        idle_icon_frame = QFrame(objectName="SubCardFrame")
        idle_icon_layout = QVBoxLayout(idle_icon_frame)
        idle_icon_layout.setContentsMargins(10, 8, 10, 8)
        idle_icon_layout.setSpacing(6)

        idle_icon_title = QLabel("\U0001f3a8 Custom Idle Presence Icon", objectName="SettingRowLabel")
        idle_icon_title.setFont(ui_font(12, QFont.Weight.DemiBold))
        idle_icon_layout.addWidget(idle_icon_title)

        idle_icon_row = QHBoxLayout()
        idle_icon_row.setSpacing(8)
        self._idle_icon_edit = QLineEdit(getattr(s, "idle_custom_image", ""))
        self._idle_icon_edit.setFont(mono_font(11))
        self._idle_icon_edit.setPlaceholderText("Contoh: idle_cyber atau https://i.imgur.com/example.png")
        self._idle_icon_edit.editingFinished.connect(
            lambda: self._state.update_settings(idle_custom_image=self._idle_icon_edit.text().strip())
        )
        self._reset_idle_icon_btn = QPushButton("Reset ke Default / Weather")
        self._reset_idle_icon_btn.setFont(ui_font(10, QFont.Weight.DemiBold))
        self._reset_idle_icon_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reset_idle_icon_btn.clicked.connect(self._on_reset_idle_icon_clicked)
        idle_icon_row.addWidget(self._idle_icon_edit, 1)
        idle_icon_row.addWidget(self._reset_idle_icon_btn)
        idle_icon_layout.addLayout(idle_icon_row)

        idle_icon_help = QLabel(
            "Kosongkan jika ingin menggunakan ikon cuaca & langit otomatis. "
            "Masukkan Asset Key dari Discord Developer Portal atau link HTTPS gambar.",
            objectName="SectionCaption",
        )
        idle_icon_help.setFont(ui_font(10))
        idle_icon_help.setWordWrap(True)
        idle_icon_layout.addWidget(idle_icon_help)
        self._celestial_card.add_block(idle_icon_frame)

        # Compact Live Weather Preview Banner
        self._weather_preview_lbl = QLabel("", objectName="WeatherPreviewBanner")
        self._weather_preview_lbl.setFont(mono_font(11, QFont.Weight.DemiBold))
        self._weather_preview_lbl.setStyleSheet(
            "background-color: rgba(56, 189, 248, 0.10); color: #38BDF8; "
            "border: 1px solid rgba(56, 189, 248, 0.28); border-radius: 8px; padding: 7px 11px;"
        )
        self._celestial_card.add_block(self._weather_preview_lbl)
        self._refresh_weather_preview()
        CELESTIAL_SYNC.signals.weather_updated.connect(lambda _snap: self._refresh_weather_preview())
        CELESTIAL_SYNC.signals.location_resolved.connect(self._on_location_resolved)

        layout.addWidget(self._celestial_card)

        # Card 4: General App Preferences
        self._general_card = SettingsCard(
            "\u2699\ufe0f General Application Preferences",
            "Configure profile card identity overrides, Windows startup, and system tray behavior.",
            has_toggle=False,
            checked=True,
            badge_text="Core Engine",
            parent=content,
        )

        prof_box = QWidget()
        prof_layout = QHBoxLayout(prof_box)
        prof_layout.setContentsMargins(0, 0, 0, 0)
        prof_layout.setSpacing(6)
        self._prof_name_edit = QLineEdit(s.profile_display_name)
        self._prof_name_edit.setPlaceholderText("Auto-detect from IPC")
        self._prof_handle_edit = QLineEdit(s.profile_handle)
        self._prof_handle_edit.setPlaceholderText("@handle")
        save_prof_btn = QPushButton("Apply")
        save_prof_btn.clicked.connect(
            lambda: self._state.update_settings(
                profile_display_name=self._prof_name_edit.text().strip(),
                profile_handle=self._prof_handle_edit.text().strip(),
            )
        )
        prof_layout.addWidget(self._prof_name_edit)
        prof_layout.addWidget(self._prof_handle_edit)
        prof_layout.addWidget(save_prof_btn)
        self._general_card.add_row("Profile Card Display Name & Handle", prof_box)

        self._autostart_switch = ToggleSwitch(checked=s.auto_start)
        self._autostart_switch.toggled.connect(lambda v: self._state.update_settings(auto_start=v))
        self._general_card.add_row("Run OmniPresence on Windows Startup", self._autostart_switch)

        self._tray_switch = ToggleSwitch(checked=s.tray_minimized)
        self._tray_switch.toggled.connect(lambda v: self._state.update_settings(tray_minimized=v))
        self._general_card.add_row("Keep Running in System Tray on Close", self._tray_switch)

        layout.addWidget(self._general_card)
        layout.addStretch(1)

        self.setWidget(content)

    def _refresh_gateway_ui(self, use_custom: bool, custom_id: str) -> None:
        use_custom = bool(use_custom)
        cleaned_custom = (custom_id or "").strip()
        if use_custom:
            self._gateway_status_badge.setText("[\u26a1] Custom Application ID Active")
            self._gateway_status_badge.setProperty("official", "false")
            self._client_id_edit.setEnabled(True)
            self._client_id_edit.setReadOnly(False)
            self._apply_gateway_btn.setEnabled(True)
            if cleaned_custom and self._client_id_edit.text() == DEFAULT_CLIENT_ID:
                self._client_id_edit.setText(cleaned_custom)
            elif not cleaned_custom and self._client_id_edit.text() == DEFAULT_CLIENT_ID:
                self._client_id_edit.clear()
        else:
            self._gateway_status_badge.setText("[\u2713] Connected via Official OmniPresence Gateway")
            self._gateway_status_badge.setProperty("official", "true")
            self._client_id_edit.setText(DEFAULT_CLIENT_ID)
            self._client_id_edit.setReadOnly(True)
            self._client_id_edit.setEnabled(False)
            self._apply_gateway_btn.setEnabled(False)
        self._gateway_status_badge.style().unpolish(self._gateway_status_badge)
        self._gateway_status_badge.style().polish(self._gateway_status_badge)

    def _on_custom_gateway_toggled(self, checked: bool) -> None:
        custom_val = self._state.settings.custom_client_id
        self._refresh_gateway_ui(checked, custom_val)
        if not checked:
            self._state.configure_gateway(False, custom_val)
        elif custom_val.strip().isdigit():
            self._state.configure_gateway(True, custom_val.strip())

    def _on_apply_gateway_clicked(self) -> None:
        raw_id = self._client_id_edit.text().strip()
        if self._use_custom_id_switch.isChecked():
            self._state.configure_gateway(True, raw_id)
        else:
            self._state.configure_gateway(False, self._state.settings.custom_client_id)

    def _on_reset_idle_icon_clicked(self) -> None:
        self._idle_icon_edit.clear()
        self._state.update_settings(idle_custom_image="")
        self._state.log("info", "celestial", "Custom Idle Icon reset to dynamic weather/celestial icon")

    def _on_location_mode_toggled(self, gps_checked: bool) -> None:
        mode = "gps" if gps_checked else "manual"
        self._state.update_settings(location_mode=mode)
        if gps_checked:
            CELESTIAL_SYNC.trigger_refresh(force_refresh=True)
        self._refresh_weather_preview()

    def _on_detect_gps_clicked(self) -> None:
        self._radio_gps.setChecked(True)
        self._state.update_settings(location_mode="gps")
        loc = CELESTIAL_SYNC.detect_gps_now()
        self._on_location_resolved(loc)
        self._state.log(
            "info",
            "celestial",
            f"Location detected ({loc.source.upper()}): {loc.city_name} ({loc.latitude:.4f}, {loc.longitude:.4f})",
        )

    def _on_search_city_clicked(self) -> None:
        query = self._manual_city_edit.text().strip()
        if not query:
            self._state.log("warn", "celestial", "Masukkan nama kota/kecamatan terlebih dahulu")
            return
        self._radio_manual.setChecked(True)
        try:
            loc = CELESTIAL_SYNC.set_manual_city(query)
        except Exception as exc:
            self._state.log("error", "celestial", f"Geocoding error for '{query}': {exc}")
            return
        if loc is None:
            self._state.log("warn", "celestial", f"Kota '{query}' tidak ditemukan di Open-Meteo Geocoding")
            return
        self._manual_city_edit.setText(loc.city_name)
        self._on_location_resolved(loc)
        self._state.log(
            "info",
            "celestial",
            f"Manual city set: {loc.city_name} ({loc.latitude:.4f}, {loc.longitude:.4f})",
        )

    def _on_location_resolved(self, loc) -> None:
        self._gps_coords_lbl.setText(
            f"Lat: {loc.latitude:.4f}, Lon: {loc.longitude:.4f} ({loc.city_name} \u2022 {loc.source.upper()})"
        )
        self._lat_spin.blockSignals(True)
        self._lon_spin.blockSignals(True)
        self._lat_spin.setValue(loc.latitude)
        self._lon_spin.setValue(loc.longitude)
        self._lat_spin.blockSignals(False)
        self._lon_spin.blockSignals(False)
        self._refresh_weather_preview()

    def _refresh_weather_preview(self) -> None:
        snap = CELESTIAL_SYNC.snapshot(allow_network=False)
        temp_str = snap.temp_c.replace("\u00b0C", "").strip() or "24"
        self._weather_preview_lbl.setText(
            f"Current Weather: {temp_str}\u00b0C \u2022 {snap.weather_desc} ({snap.city_name})"
        )

    def sync_from_settings(self, s) -> None:
        self._shield_card.setChecked(s.privacy_shield_enabled)
        self._mood_card.setChecked(s.mood_engine_enabled)
        self._celestial_card.setChecked(s.celestial_sync_enabled)
        self._weather_switch.blockSignals(True)
        self._weather_switch.setChecked(s.celestial_weather_enabled, animate=False)
        self._weather_switch.blockSignals(False)
        self._show_city_switch.blockSignals(True)
        self._show_city_switch.setChecked(getattr(s, "privacy_show_city", False), animate=False)
        self._show_city_switch.blockSignals(False)
        custom_img = getattr(s, "idle_custom_image", "")
        if self._idle_icon_edit.text() != custom_img:
            self._idle_icon_edit.setText(custom_img)
        is_manual = getattr(s, "location_mode", "gps").strip().lower() == "manual"
        self._radio_manual.blockSignals(True)
        self._radio_gps.blockSignals(True)
        self._radio_manual.setChecked(is_manual)
        self._radio_gps.setChecked(not is_manual)
        self._radio_manual.blockSignals(False)
        self._radio_gps.blockSignals(False)
        if getattr(s, "manual_location_name", "") and self._manual_city_edit.text() != s.manual_location_name:
            self._manual_city_edit.setText(s.manual_location_name)
        self._tray_switch.blockSignals(True)
        self._tray_switch.setChecked(s.tray_minimized, animate=False)
        self._tray_switch.blockSignals(False)

        use_custom = bool(getattr(s, "use_custom_client_id", False))
        custom_id = getattr(s, "custom_client_id", "")
        self._use_custom_id_switch.blockSignals(True)
        self._use_custom_id_switch.setChecked(use_custom, animate=False)
        self._use_custom_id_switch.blockSignals(False)
        self._refresh_gateway_ui(use_custom, custom_id)
        self._refresh_weather_preview()


class CategoryDistributionBar(QWidget):
    """Multi-segment horizontal bar visualizing percentage breakdown across Coding, Media, Gaming, Browsing, Idle."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(16)
        self._percentages: dict[str, float] = {c: 0.0 for c in CATEGORIES}

    def set_percentages(self, percentages: dict[str, float]) -> None:
        self._percentages = dict(percentages)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        w = float(self.width())
        h = float(self.height())
        painter.setBrush(QColor("#0D1117"))
        painter.drawRoundedRect(QRectF(0, 0, w, h), 7.0, 7.0)

        total_pct = sum(max(0.0, self._percentages.get(c, 0.0)) for c in CATEGORIES)
        if total_pct <= 0.0:
            painter.setBrush(QColor("#1A212C"))
            painter.drawRoundedRect(QRectF(0, 0, w, h), 7.0, 7.0)
            return

        x = 0.0
        for cat in CATEGORIES:
            pct = max(0.0, self._percentages.get(cat, 0.0))
            if pct <= 0.0:
                continue
            seg_w = (pct / total_pct) * w
            if seg_w < 2.0:
                continue
            painter.setBrush(QColor(CATEGORY_COLORS.get(cat, "#64748B")))
            painter.drawRoundedRect(QRectF(x, 0, seg_w, h), 5.0, 5.0)
            x += seg_w


class SingleCategoryBar(QWidget):
    """Smooth progress bar for a single category row inside AnalyticsPage."""

    def __init__(self, color_hex: str, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(8)
        self._color = QColor(color_hex)
        self._pct = 0.0

    def set_percentage(self, pct: float) -> None:
        self._pct = min(max(float(pct), 0.0), 100.0)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        w = float(self.width())
        h = float(self.height())
        painter.setBrush(QColor("#0D1117"))
        painter.drawRoundedRect(QRectF(0, 0, w, h), 4.0, 4.0)

        fill_w = (self._pct / 100.0) * w
        if fill_w > 1.5:
            painter.setBrush(self._color)
            painter.drawRoundedRect(QRectF(0, 0, max(4.0, fill_w), h), 4.0, 4.0)


class AnalyticsPage(QScrollArea):
    """Page 3 (Index 3): Mini Timeline & Day in Review Analytics Dashboard."""

    def __init__(self, state: StateManager, parent=None) -> None:
        super().__init__(parent, objectName="SettingsScroll")
        self._state = state
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget(objectName="WindowCanvas")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 2, 10, 12)
        layout.setSpacing(14)

        # 1. Hero Summary Card: Day in Review
        hero_card = QFrame(objectName="SettingsCard")
        hero_layout = QVBoxLayout(hero_card)
        hero_layout.setContentsMargins(16, 14, 16, 14)
        hero_layout.setSpacing(10)

        top_row = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title_lbl = QLabel("\U0001f4ca Mini Timeline & Day in Review", objectName="CardTitle")
        title_lbl.setFont(ui_font(14, QFont.Weight.Bold))
        self._date_sub_lbl = QLabel("Daily Activity Telemetry \u2022 Persisted to data/analytics.json", objectName="SectionCaption")
        self._date_sub_lbl.setFont(ui_font(11))
        title_col.addWidget(title_lbl)
        title_col.addWidget(self._date_sub_lbl)
        top_row.addLayout(title_col, 1)

        reset_btn = QPushButton("Reset Today")
        reset_btn.setFont(ui_font(10, QFont.Weight.DemiBold))
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.clicked.connect(self._on_reset_today)
        top_row.addWidget(reset_btn, 0, Qt.AlignmentFlag.AlignTop)
        hero_layout.addLayout(top_row)

        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(12)

        self._total_active_lbl = QLabel("Total Active: 0s", objectName="CardTitle")
        self._total_active_lbl.setFont(mono_font(16, QFont.Weight.Bold))
        self._total_active_lbl.setStyleSheet("color: #00E5FF;")
        metrics_row.addWidget(self._total_active_lbl)

        self._total_tracked_lbl = QLabel("Tracked Today: 0s", objectName="SectionCaption")
        self._total_tracked_lbl.setFont(mono_font(11, QFont.Weight.DemiBold))
        metrics_row.addWidget(self._total_tracked_lbl)
        metrics_row.addStretch(1)

        self._active_cat_pill = QLabel("Active: Idle", objectName="CardPillBadge")
        self._active_cat_pill.setFont(mono_font(10, QFont.Weight.DemiBold))
        metrics_row.addWidget(self._active_cat_pill)
        hero_layout.addLayout(metrics_row)

        self._dist_bar = CategoryDistributionBar(hero_card)
        hero_layout.addWidget(self._dist_bar)

        layout.addWidget(hero_card)

        # 2. Category Breakdown Card
        cat_card = QFrame(objectName="SettingsCard")
        cat_layout = QVBoxLayout(cat_card)
        cat_layout.setContentsMargins(16, 14, 16, 14)
        cat_layout.setSpacing(10)

        cat_hdr = QLabel("Category Distribution (Coding \u2022 Media \u2022 Gaming \u2022 Browsing \u2022 Idle)", objectName="CardTitle")
        cat_hdr.setFont(ui_font(13, QFont.Weight.Bold))
        cat_layout.addWidget(cat_hdr)

        self._cat_bars: dict[str, SingleCategoryBar] = {}
        self._cat_stats_lbls: dict[str, QLabel] = {}

        for cat in CATEGORIES:
            row_w = QFrame(objectName="SubCardFrame")
            row_l = QVBoxLayout(row_w)
            row_l.setContentsMargins(10, 7, 10, 7)
            row_l.setSpacing(5)

            hdr = QHBoxLayout()
            col_hex = CATEGORY_COLORS.get(cat, "#00E5FF")
            icon = CATEGORY_ICONS.get(cat, "\u2022")
            name_lbl = QLabel(f"{icon}  {cat}")
            name_lbl.setFont(ui_font(11, QFont.Weight.DemiBold))
            name_lbl.setStyleSheet(f"color: {col_hex};")
            stat_lbl = QLabel("0s (0%)", objectName="SectionCaption")
            stat_lbl.setFont(mono_font(10, QFont.Weight.DemiBold))
            hdr.addWidget(name_lbl)
            hdr.addStretch(1)
            hdr.addWidget(stat_lbl)
            row_l.addLayout(hdr)

            bar = SingleCategoryBar(col_hex, row_w)
            row_l.addWidget(bar)
            cat_layout.addWidget(row_w)

            self._cat_bars[cat] = bar
            self._cat_stats_lbls[cat] = stat_lbl

        layout.addWidget(cat_card)

        # 3. Recent Sessions Timeline Card
        sess_card = QFrame(objectName="SettingsCard")
        sess_layout = QVBoxLayout(sess_card)
        sess_layout.setContentsMargins(16, 14, 16, 14)
        sess_layout.setSpacing(8)

        sess_hdr = QLabel("\u23f1 Recent Sessions Timeline (Today)", objectName="CardTitle")
        sess_hdr.setFont(ui_font(13, QFont.Weight.Bold))
        sess_layout.addWidget(sess_hdr)

        self._sessions_host = QVBoxLayout()
        self._sessions_host.setSpacing(6)
        sess_layout.addLayout(self._sessions_host)

        layout.addWidget(sess_card)
        layout.addStretch(1)

        self.setWidget(content)
        self.refresh_analytics(ANALYTICS_MANAGER.snapshot())

    def _on_reset_today(self) -> None:
        snap = ANALYTICS_MANAGER.reset_today()
        self.refresh_analytics(snap)
        self._state.log("info", "analytics", "Daily analytics reset for today")

    def refresh_analytics(self, snap: DailyAnalyticsSnapshot | None = None) -> None:
        snap = snap or ANALYTICS_MANAGER.snapshot()
        self._date_sub_lbl.setText(f"Date: {snap.date_str} \u2022 Saved to data/analytics.json")
        self._total_active_lbl.setText(f"Total Active: {format_analytics_duration(snap.total_active_seconds)}")
        self._total_tracked_lbl.setText(f"Total Tracked: {format_analytics_duration(snap.total_tracked_seconds)}")

        icon = CATEGORY_ICONS.get(snap.active_category, "\u2022")
        self._active_cat_pill.setText(f"{icon} {snap.active_category}")

        pcts = snap.percentages()
        self._dist_bar.set_percentages(pcts)

        for cat in CATEGORIES:
            sec = snap.totals.get(cat, 0.0)
            pct = pcts.get(cat, 0.0)
            self._cat_bars[cat].set_percentage(pct)
            self._cat_stats_lbls[cat].setText(f"{format_analytics_duration(sec)} ({pct:.0f}%)")

        # Update recent session rows (show top 8)
        while self._sessions_host.count():
            item = self._sessions_host.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not snap.sessions:
            empty = QLabel("No sessions recorded yet today.", objectName="SectionCaption")
            empty.setFont(ui_font(11))
            self._sessions_host.addWidget(empty)
            return

        for rec in snap.sessions[:8]:
            row = QFrame(objectName="SubCardFrame")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(10, 6, 10, 6)
            rl.setSpacing(10)

            ts_str = datetime.fromtimestamp(rec.started_at).strftime("%H:%M:%S")
            ts_lbl = QLabel(ts_str, objectName="SectionCaption")
            ts_lbl.setFont(mono_font(10))
            rl.addWidget(ts_lbl)

            col = CATEGORY_COLORS.get(rec.category, "#00E5FF")
            pill = QLabel(rec.category)
            pill.setFont(mono_font(9, QFont.Weight.Bold))
            pill.setStyleSheet(
                f"background-color: {col}22; color: {col}; border: 1px solid {col}55; "
                "border-radius: 5px; padding: 1px 7px;"
            )
            rl.addWidget(pill)

            head_lbl = QLabel(rec.headline)
            head_lbl.setFont(ui_font(11, QFont.Weight.DemiBold))
            rl.addWidget(head_lbl, 1)

            dur_lbl = QLabel(format_analytics_duration(rec.duration_seconds), objectName="SectionCaption")
            dur_lbl.setFont(mono_font(10, QFont.Weight.DemiBold))
            rl.addWidget(dur_lbl)

            self._sessions_host.addWidget(row)


class SidebarWidget(QFrame):
    """Collapsible Left Navigation Sidebar (220px Expanded <-> 68px Compact Icon-only) with 5 navigation items."""

    EXPANDED_WIDTH = 220
    COLLAPSED_WIDTH = 68
    ANIMATION_MS = 200

    page_selected = Signal(int)
    tray_toggled = Signal(bool)
    collapsed_changed = Signal(bool)

    NAV_ITEMS = (
        (0, "\u2756", "Overview", "Overview \u2014 Live Discord Preview & Quick Controls"),
        (1, "\u2630", "Context Rules", "Context Rules \u2014 P2\u2013P6 Application Detection"),
        (2, "\u2726", "Custom RPC Studio", "Custom RPC Studio \u2014 P1 Manual Override"),
        (3, "\U0001f4ca", "Analytics", "Analytics \u2014 Mini Timeline & Day in Review"),
        (4, "\u2699", "Settings", "Settings \u2014 Gateway, Shield, Mood, Celestial & Preferences"),
    )

    def __init__(self, app_name: str, tray_minimized: bool = True, parent=None) -> None:
        super().__init__(parent, objectName="Sidebar")
        self._app_name = app_name
        self._collapsed = False
        self._active_index = 0
        self.setMinimumWidth(self.EXPANDED_WIDTH)
        self.setMaximumWidth(self.EXPANDED_WIDTH)

        self._width_anim = QParallelAnimationGroup(self)
        self._min_anim = QPropertyAnimation(self, b"minimumWidth", self)
        self._max_anim = QPropertyAnimation(self, b"maximumWidth", self)
        for anim in (self._min_anim, self._max_anim):
            anim.setDuration(self.ANIMATION_MS)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._width_anim.addAnimation(anim)

        self._root_layout = QVBoxLayout(self)
        self._root_layout.setContentsMargins(12, 12, 12, 14)
        self._root_layout.setSpacing(8)

        # Sidebar Top Control Row: Subtle section label + Chevron Collapse Button (no duplicate logo/version)
        header_row = QHBoxLayout()
        header_row.setSpacing(6)
        self._sub = QLabel("NAVIGATION", objectName="SectionCaption")
        self._sub.setFont(mono_font(10, QFont.Weight.Bold))
        header_row.addWidget(self._sub)
        header_row.addStretch(1)

        self._collapse_btn = QPushButton("\u2039", objectName="SidebarCollapseButton")
        self._collapse_btn.setFixedSize(28, 26)
        self._collapse_btn.setFont(ui_font(14, QFont.Weight.Bold))
        self._collapse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._collapse_btn.setToolTip("Collapse Sidebar (68px Compact Mode)")
        self._collapse_btn.clicked.connect(self.toggle_collapsed)
        header_row.addWidget(self._collapse_btn)
        self._root_layout.addLayout(header_row)
        self._root_layout.addSpacing(4)

        self._nav_buttons: dict[int, QPushButton] = {}
        for idx, icon_only, title_text, tip in self.NAV_ITEMS:
            btn = QPushButton(self._format_nav_label(idx, idx == 0, False), objectName="SidebarNavButton")
            btn.setFont(ui_font(12, QFont.Weight.DemiBold))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(tip)
            btn.setProperty("active", "true" if idx == 0 else "false")
            btn.clicked.connect(lambda _checked=False, i=idx: self.set_active_page(i))
            self._root_layout.addWidget(btn)
            self._nav_buttons[idx] = btn

        self._root_layout.addStretch(1)

        # Unified Sidebar Footer: Blinking StatusDot + Connected (@handle) + Minimize to Tray ToggleSwitch
        self._footer = QFrame(objectName="SidebarFooter")
        f_layout = QVBoxLayout(self._footer)
        f_layout.setContentsMargins(10, 9, 10, 9)
        f_layout.setSpacing(7)

        ipc_row = QHBoxLayout()
        ipc_row.setSpacing(7)
        self._dot = StatusDot(self._footer)
        self._ipc_label = QLabel("IPC Disconnected", objectName="StatusText")
        self._ipc_label.setFont(mono_font(10, QFont.Weight.DemiBold))
        ipc_row.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignCenter)
        ipc_row.addWidget(self._ipc_label, 1)
        f_layout.addLayout(ipc_row)

        tray_row = QHBoxLayout()
        self._tray_lbl = QLabel("Minimize to Tray", objectName="SectionCaption")
        self._tray_lbl.setFont(ui_font(11))
        self._tray_switch = ToggleSwitch(checked=tray_minimized, parent=self._footer)
        self._tray_switch.toggled.connect(self.tray_toggled.emit)
        tray_row.addWidget(self._tray_lbl, 1)
        tray_row.addWidget(self._tray_switch)
        f_layout.addLayout(tray_row)

        self._root_layout.addWidget(self._footer)

    def _format_nav_label(self, idx: int, is_active: bool, collapsed: bool) -> str:
        _, icon_only, title_text, _ = self.NAV_ITEMS[idx]
        if collapsed:
            return icon_only
        dot_prefix = "\u25cf " if is_active else ""
        return f"{dot_prefix}{icon_only}   {title_text}"

    @property
    def is_collapsed(self) -> bool:
        return self._collapsed

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self._collapsed, animate=True)

    def set_collapsed(self, collapsed: bool, *, animate: bool = True) -> None:
        collapsed = bool(collapsed)
        if collapsed == self._collapsed and self._width_anim.state() != QParallelAnimationGroup.State.Running:
            return
        self._collapsed = collapsed
        target_w = self.COLLAPSED_WIDTH if collapsed else self.EXPANDED_WIDTH
        current_w = self.width()

        # Update child visibility and button text/alignment
        self._sub.setVisible(not collapsed)
        self._ipc_label.setVisible(not collapsed)
        self._tray_lbl.setVisible(not collapsed)
        self._tray_switch.setVisible(not collapsed)

        self._collapse_btn.setText("\u203a" if collapsed else "\u2039")
        self._collapse_btn.setToolTip(
            "Expand Sidebar (220px Full Mode)" if collapsed else "Collapse Sidebar (68px Compact Mode)"
        )
        self._root_layout.setContentsMargins(8 if collapsed else 12, 12, 8 if collapsed else 12, 14)

        for idx, _icon_only, _title_text, _tip in self.NAV_ITEMS:
            btn = self._nav_buttons[idx]
            btn.setText(self._format_nav_label(idx, idx == self._active_index, collapsed))
            btn.setProperty("collapsed", "true" if collapsed else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

        self._width_anim.stop()
        if animate and self.isVisible():
            for anim in (self._min_anim, self._max_anim):
                anim.setStartValue(current_w)
                anim.setEndValue(target_w)
            self._width_anim.start()
        else:
            self.setMinimumWidth(target_w)
            self.setMaximumWidth(target_w)

        self.collapsed_changed.emit(collapsed)

    def set_active_page(self, index: int) -> None:
        self._active_index = index
        for idx, btn in self._nav_buttons.items():
            is_act = idx == index
            btn.setText(self._format_nav_label(idx, is_act, self._collapsed))
            btn.setProperty("active", "true" if is_act else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self.page_selected.emit(index)

    def set_connection(self, status: str, detail: str) -> None:
        self._dot.set_status(status.lower())
        self._dot.setToolTip(detail or f"IPC {status.capitalize()}")
        short = detail if len(detail) <= 24 else f"IPC {status.capitalize()}"
        self._ipc_label.setText(short or f"IPC {status.capitalize()}")
        self._ipc_label.setToolTip(detail)

    def set_tray_checked(self, checked: bool) -> None:
        self._tray_switch.blockSignals(True)
        self._tray_switch.setChecked(checked, animate=False)
        self._tray_switch.blockSignals(False)


class UpdateBannerWidget(QFrame):
    """Slim semi-transparent top notification banner for GitHub Release auto-updates."""

    update_requested = Signal(str)   # download_url
    restart_requested = Signal(str)  # temp_file_path

    def __init__(self, parent=None) -> None:
        super().__init__(parent, objectName="UpdateBanner")
        self._download_url: str = ""
        self._temp_file_path: str = ""
        self._ready_to_apply: bool = False

        self.setFixedHeight(40)
        self.setVisible(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 4, 14, 4)
        layout.setSpacing(10)

        self._msg_label = QLabel(
            "\u2728 Pembaruan tersedia! Klik untuk memperbarui.",
            objectName="UpdateBannerText",
        )
        self._msg_label.setFont(ui_font(12, QFont.Weight.DemiBold))
        layout.addWidget(self._msg_label, 1)

        self._progress_bar = QProgressBar(objectName="UpdateProgressBar")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFixedWidth(160)
        self._progress_bar.setFixedHeight(14)
        self._progress_bar.setVisible(False)
        layout.addWidget(self._progress_bar, 0)

        self._action_btn = QPushButton("Update Now", objectName="PrimaryButton")
        self._action_btn.setFont(ui_font(11, QFont.Weight.Bold))
        self._action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._action_btn.setFixedHeight(26)
        self._action_btn.clicked.connect(self._on_action_clicked)
        layout.addWidget(self._action_btn, 0)

        self._ignore_btn = QPushButton("\u2715", objectName="TitleButton")
        self._ignore_btn.setFont(ui_font(11, QFont.Weight.Bold))
        self._ignore_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ignore_btn.setToolTip("Ignore update for this session")
        self._ignore_btn.clicked.connect(lambda: self.setVisible(False))
        layout.addWidget(self._ignore_btn, 0)

    def show_update_available(self, new_version: str, changelog: str, download_url: str) -> None:
        ver_display = new_version if new_version.lower().startswith("v") else f"v{new_version}"
        self._download_url = (download_url or "").strip()
        self._temp_file_path = ""
        self._ready_to_apply = False
        self._msg_label.setText(f"\u2728 Pembaruan {ver_display} tersedia! Klik untuk memperbarui.")
        if changelog:
            self._msg_label.setToolTip(changelog[:800])
        self._progress_bar.setVisible(False)
        self._progress_bar.setValue(0)
        self._action_btn.setEnabled(True)
        self._action_btn.setText("Update Now")
        self.setVisible(True)

    def set_download_progress(self, percent: int) -> None:
        clamped = min(max(int(percent), 0), 100)
        self._progress_bar.setVisible(True)
        self._progress_bar.setValue(clamped)
        self._action_btn.setEnabled(False)
        self._action_btn.setText(f"Downloading ({clamped}%)...")

    def show_ready_to_apply(self, temp_file_path: str) -> None:
        self._temp_file_path = temp_file_path
        self._ready_to_apply = True
        self._progress_bar.setVisible(False)
        self._msg_label.setText("\u2728 Pembaruan siap dipasang. Restart sekarang?")
        self._action_btn.setEnabled(True)
        self._action_btn.setText("Restart & Apply")
        self.setVisible(True)

    def _on_action_clicked(self) -> None:
        if self._ready_to_apply and self._temp_file_path:
            self.restart_requested.emit(self._temp_file_path)
        elif self._download_url:
            self.update_requested.emit(self._download_url)


class MainWindow(QMainWindow):
    def __init__(self, state: StateManager, tray_minimized: bool = True) -> None:
        super().__init__()
        self._state = state
        self._tray_minimized = tray_minimized
        self._force_quit = False

        app_name = state.settings.discord_app_name or "OmniPresence"
        self.setWindowTitle(f"{app_name} v2.3.0")
        self.setWindowIcon(app_icon("disconnected"))
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        self.resize(1160, 760)
        self.setMinimumSize(980, 660)

        central_root = QWidget(self, objectName="centralWidget")
        shell_layout = QVBoxLayout(central_root)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        self._titlebar = CustomTitleBar(self, app_name)
        self._titlebar.minimize_clicked.connect(self.showMinimized)
        self._titlebar.maximize_clicked.connect(self._toggle_maximize)
        self._titlebar.close_clicked.connect(self.close)
        shell_layout.addWidget(self._titlebar)

        # Slim Auto-Update Notification Banner at top of dashboard
        self._update_banner = UpdateBannerWidget(central_root)
        self._update_banner.update_requested.connect(self._on_start_update_download)
        self._update_banner.restart_requested.connect(self._on_apply_update_restart)
        shell_layout.addWidget(self._update_banner)

        workspace = QWidget(objectName="mainContainer")
        workspace_layout = QHBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(0)

        # Left Collapsible Navigation Sidebar (220px <-> 68px)
        self._sidebar = SidebarWidget(app_name, state.settings.tray_minimized, workspace)
        self._sidebar.tray_toggled.connect(self._on_sidebar_tray_toggled)
        workspace_layout.addWidget(self._sidebar, 0)

        # Right AnimatedStackedWidget (5 Pages with Smooth Fade + Subtle Slide)
        self._stack = AnimatedStackedWidget(workspace)
        self._sidebar.page_selected.connect(self._stack.setCurrentIndex)

        # --- Page 0: Overview (Live Discord Preview + QuickControls + Tier Status Grid + Console Log) ---
        page_overview = QWidget(objectName="WindowCanvas")
        ov_layout = QHBoxLayout(page_overview)
        ov_layout.setContentsMargins(14, 14, 14, 14)
        ov_layout.setSpacing(14)

        left_col = QFrame(objectName="PanelCard")
        left_col.setFixedWidth(348)
        left_layout = QVBoxLayout(left_col)
        left_layout.setContentsMargins(12, 12, 12, 12)
        left_layout.setSpacing(10)

        preview_title = QLabel("Live Discord Card Preview", objectName="SectionHeading")
        preview_title.setFont(ui_font(13, QFont.Weight.DemiBold))
        left_layout.addWidget(preview_title)

        display_name, handle = state.profile
        self._preview = DiscordPreview(app_name, display_name, handle, left_col)
        left_layout.addWidget(self._preview, 1)
        ov_layout.addWidget(left_col, 0)

        right_col = QVBoxLayout()
        right_col.setSpacing(12)

        active_client_id = state.settings.get_active_client_id()
        self._controls = QuickControls(active_client_id, page_overview)
        self._controls.manual_submitted.connect(state.set_manual)
        self._controls.pause_toggled.connect(state.set_paused)
        self._controls.reload_requested.connect(state.reload_rules)
        self._controls.client_id_submitted.connect(state.set_client_id)
        right_col.addWidget(self._controls, 0)

        self._tier_grid = TierStatusGrid(page_overview)
        self._tier_grid.set_shield_state(state.shield_active, state.settings.privacy_shield_enabled)
        right_col.addWidget(self._tier_grid, 0)

        self._logs = ActivityLogPanel(state.settings.log_capacity, page_overview)
        for existing in state.log_history:
            self._logs.append_entry(existing)
        right_col.addWidget(self._logs, 1)

        ov_layout.addLayout(right_col, 1)
        self._stack.addWidget(page_overview)  # Index 0

        # --- Page 1: Context Rules (P2-P6 Table) ---
        page_rules = QWidget(objectName="WindowCanvas")
        pr_layout = QVBoxLayout(page_rules)
        pr_layout.setContentsMargins(14, 14, 14, 14)
        self._rules = RuleListWidget(state.rulebook, page_rules)
        self._rules.set_rulebook(state.rulebook, state.rule_error)
        self._rules.rule_toggled.connect(state.set_rule_enabled)
        pr_layout.addWidget(self._rules, 1)
        self._stack.addWidget(page_rules)  # Index 1

        # --- Page 2: Custom RPC Studio (PreMiD-Style Tier P1) ---
        page_custom = QWidget(objectName="WindowCanvas")
        pc_layout = QVBoxLayout(page_custom)
        pc_layout.setContentsMargins(14, 14, 14, 14)
        presets_path = state._rules_path.parent / "custom_presets.json"
        self._custom_rpc = CustomRPCPanel(presets_path, active_client_id, page_custom)
        self._custom_rpc.custom_rpc_submitted.connect(state.set_custom_rpc)
        pc_layout.addWidget(self._custom_rpc, 0)
        pc_layout.addStretch(1)
        self._stack.addWidget(page_custom)  # Index 2

        # --- Page 3: Analytics (Mini Timeline & Day in Review) ---
        page_analytics = QWidget(objectName="WindowCanvas")
        pa_layout = QVBoxLayout(page_analytics)
        pa_layout.setContentsMargins(14, 14, 14, 14)
        self._analytics_page = AnalyticsPage(state, page_analytics)
        pa_layout.addWidget(self._analytics_page, 1)
        self._stack.addWidget(page_analytics)  # Index 3

        # --- Page 4: Settings (Gateway, Privacy Shield, Mood Engine, Celestial Sync, General) ---
        page_settings = QWidget(objectName="WindowCanvas")
        ps_layout = QVBoxLayout(page_settings)
        ps_layout.setContentsMargins(14, 14, 14, 14)
        self._settings_page = SettingsPage(state, page_settings)
        ps_layout.addWidget(self._settings_page, 1)
        self._stack.addWidget(page_settings)  # Index 4

        workspace_layout.addWidget(self._stack, 1)
        shell_layout.addWidget(workspace, 1)
        self.setCentralWidget(central_root)

        # Wire reactive StateManager signals
        state.presence_changed.connect(self._on_presence_changed)
        state.connection_changed.connect(self._on_connection_changed)
        state.telemetry_changed.connect(self._titlebar.set_telemetry)
        state.log_added.connect(self._logs.append_entry)
        state.rules_changed.connect(self._rules.set_rulebook)
        state.paused_changed.connect(self._on_paused_changed)
        state.manual_changed.connect(self._controls.set_manual_state)
        state.custom_rpc_changed.connect(self._custom_rpc.set_custom_rpc_state)
        state.client_id_changed.connect(self._controls.set_client_id_text)
        state.profile_changed.connect(self._preview.set_profile)
        state.shield_changed.connect(self._on_shield_changed)
        state.settings_updated.connect(self._on_settings_updated)
        state.analytics_changed.connect(self._analytics_page.refresh_analytics)

        self._on_connection_changed(*state.connection)
        if state.resolution is not None:
            self._on_presence_changed(state.resolution)

        # Background GitHub Release Auto-Updater (silent check 3.5s after startup)
        self._updater = UpdateCheckerThread(CURRENT_VERSION, parent=self)
        self._updater.update_available.connect(self._on_update_available)
        self._updater.download_progress.connect(self._update_banner.set_download_progress)
        self._updater.download_finished.connect(self._on_update_download_finished)
        self._updater.check_failed.connect(self._on_update_check_failed)
        QTimer.singleShot(3500, self._updater.start_check)

    def _on_update_available(self, new_version: str, changelog: str, download_url: str) -> None:
        self._update_banner.show_update_available(new_version, changelog, download_url)
        self._state.log("info", "updater", f"New GitHub release available: {new_version}")

    def _on_start_update_download(self, download_url: str) -> None:
        cleaned_url = (download_url or "").strip()
        if not cleaned_url:
            return
        lower_url = cleaned_url.split("?")[0].lower()
        if not (lower_url.endswith(".exe") or lower_url.endswith(".zip")):
            QDesktopServices.openUrl(QUrl(cleaned_url))
            return
        self._update_banner.set_download_progress(0)
        self._state.log("info", "updater", f"Downloading update from {cleaned_url}")
        self._updater.start_download(cleaned_url)

    def _on_update_download_finished(self, temp_file_path: str) -> None:
        self._update_banner.show_ready_to_apply(temp_file_path)
        self._state.log("info", "updater", f"Update package downloaded: {temp_file_path}")

    def _on_update_check_failed(self, error_message: str) -> None:
        # Silent failure during routine startup check; log only if banner was actively downloading
        if self._update_banner.isVisible():
            self._state.log("warn", "updater", f"Update operation failed: {error_message}")

    def _on_apply_update_restart(self, temp_file_path: str) -> None:
        self._state.log("info", "updater", "Applying update and restarting OmniPresence...")
        self.prepare_quit()
        apply_update_and_restart(temp_file_path)

    def prepare_quit(self) -> None:
        self._force_quit = True
        ANALYTICS_MANAGER.save()

    def restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _on_sidebar_tray_toggled(self, enabled: bool) -> None:
        self._tray_minimized = bool(enabled)
        self._state.update_settings(tray_minimized=bool(enabled))

    def _on_settings_updated(self, settings) -> None:
        self._tray_minimized = bool(settings.tray_minimized)
        self._sidebar.set_tray_checked(self._tray_minimized)
        self._settings_page.sync_from_settings(settings)
        self._tier_grid.set_shield_state(self._state.shield_active, settings.privacy_shield_enabled)
        self._tier_grid.refresh_celestial()

    def _on_shield_changed(self, engaged: bool) -> None:
        self._tier_grid.set_shield_state(engaged, self._state.settings.privacy_shield_enabled)

    def _on_presence_changed(self, resolution) -> None:
        self._preview.set_resolution(resolution)
        self._rules.set_resolution(resolution)
        self._tier_grid.set_resolution(resolution)

    def _on_connection_changed(self, status: str, detail: str) -> None:
        self.setWindowIcon(app_icon(status))
        self._titlebar.set_connection(status, detail)
        self._sidebar.set_connection(status, detail)
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
        ANALYTICS_MANAGER.save()
        super().closeEvent(event)


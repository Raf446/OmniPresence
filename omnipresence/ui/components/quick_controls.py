"""Action bar for manual override (P1), rule reloading, pause toggle, and Discord App ID setup."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ui import mono_font, ui_font


class QuickControls(QFrame):
    manual_submitted = Signal(bool, str, str)
    pause_toggled = Signal(bool)
    reload_requested = Signal()
    client_id_submitted = Signal(str)

    def __init__(self, initial_client_id: str = "", parent=None) -> None:
        super().__init__(parent, objectName="QuickControlsBar")
        self._manual_active = False
        self._paused = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # Row 1: Manual override (P1) + Pause + Reload rules
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        override_label = QLabel("P1 Override", objectName="SectionHeading")
        override_label.setFont(mono_font(12, QFont.Weight.DemiBold))
        top_row.addWidget(override_label)

        self._details_input = QLineEdit()
        self._details_input.setPlaceholderText("Manual details (e.g. Deep work session)")
        self._details_input.setFont(ui_font(12))
        self._details_input.returnPressed.connect(self._apply_manual)
        top_row.addWidget(self._details_input, 2)

        self._state_input = QLineEdit()
        self._state_input.setPlaceholderText("Optional state (e.g. Do not disturb)")
        self._state_input.setFont(ui_font(12))
        self._state_input.returnPressed.connect(self._apply_manual)
        top_row.addWidget(self._state_input, 2)

        self._manual_btn = QPushButton("Engage Override", objectName="PrimaryButton")
        self._manual_btn.setFont(ui_font(12, QFont.Weight.DemiBold))
        self._manual_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._manual_btn.clicked.connect(self._toggle_manual)
        top_row.addWidget(self._manual_btn)

        self._pause_btn = QPushButton("Pause IPC", objectName="ToggleButton")
        self._pause_btn.setProperty("active", "false")
        self._pause_btn.setFont(ui_font(12, QFont.Weight.DemiBold))
        self._pause_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pause_btn.clicked.connect(self._on_pause_clicked)
        top_row.addWidget(self._pause_btn)

        self._reload_btn = QPushButton("Reload Rules")
        self._reload_btn.setFont(ui_font(12))
        self._reload_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reload_btn.setToolTip("Reload config/rules.json from disk")
        self._reload_btn.clicked.connect(self.reload_requested.emit)
        top_row.addWidget(self._reload_btn)

        layout.addLayout(top_row)

        # Row 2: Discord Application ID configuration
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)

        app_id_label = QLabel("Discord App ID", objectName="SectionCaption")
        app_id_label.setFont(mono_font(11))
        bottom_row.addWidget(app_id_label)

        self._client_id_input = QLineEdit(initial_client_id)
        self._client_id_input.setPlaceholderText("18-19 digit Application ID from Discord Developer Portal")
        self._client_id_input.setFont(mono_font(12))
        self._client_id_input.returnPressed.connect(self._on_save_client_id)
        bottom_row.addWidget(self._client_id_input, 1)

        self._save_id_btn = QPushButton("Apply App ID")
        self._save_id_btn.setFont(ui_font(12))
        self._save_id_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_id_btn.clicked.connect(self._on_save_client_id)
        bottom_row.addWidget(self._save_id_btn)

        layout.addLayout(bottom_row)

    def _apply_manual(self) -> None:
        details = self._details_input.text().strip()
        state = self._state_input.text().strip()
        self.manual_submitted.emit(True, details, state)

    def _toggle_manual(self) -> None:
        if self._manual_active:
            self.manual_submitted.emit(False, "", "")
        else:
            self._apply_manual()

    def _on_pause_clicked(self) -> None:
        self.pause_toggled.emit(not self._paused)

    def _on_save_client_id(self) -> None:
        self.client_id_submitted.emit(self._client_id_input.text().strip())

    def set_manual_state(self, active: bool) -> None:
        self._manual_active = active
        self._manual_btn.setText("Release Override" if active else "Engage Override")
        self._manual_btn.setProperty("active", "true" if active else "false")
        self._manual_btn.style().unpolish(self._manual_btn)
        self._manual_btn.style().polish(self._manual_btn)

    def set_paused_state(self, paused: bool) -> None:
        self._paused = paused
        self._pause_btn.setText("Resume IPC" if paused else "Pause IPC")
        self._pause_btn.setProperty("active", "true" if paused else "false")
        self._pause_btn.style().unpolish(self._pause_btn)
        self._pause_btn.style().polish(self._pause_btn)

    def set_client_id_text(self, client_id: str) -> None:
        if self._client_id_input.text().strip() != client_id:
            self._client_id_input.setText(client_id)

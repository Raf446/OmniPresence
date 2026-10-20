"""Action bar for P1 Manual Override, Segmented Mode Selector, Pause IPC, and Reload Rules (Obsidian Glass v2.4)."""
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

    MODE_PRESETS = (
        ("Deep Work", "Deep Work Session", "Do Not Disturb • Focus Mode"),
        ("Focus Flow", "In the Flow State", "Building & Engineering"),
        ("Stealth AFK", "Away from Keyboard", "Standby • Back Soon"),
        ("Custom", "", ""),
    )

    def __init__(self, initial_client_id: str = "", parent=None) -> None:
        super().__init__(parent, objectName="QuickControlsBar")
        self._manual_active = False
        self._paused = False
        self._client_id = initial_client_id

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        # Row 1: Header + Segmented Mode Selector Pills
        header_row = QHBoxLayout()
        header_row.setSpacing(8)

        override_label = QLabel("Advanced Overrides & Controls", objectName="SectionHeading")
        override_label.setFont(ui_font(12, QFont.Weight.Bold))
        header_row.addWidget(override_label)

        tier_pill = QLabel("P1 Priority", objectName="CardPillBadge")
        tier_pill.setFont(mono_font(10, QFont.Weight.DemiBold))
        header_row.addWidget(tier_pill)
        header_row.addStretch(1)

        self._mode_pills: list[QPushButton] = []
        for idx, (label, details_val, state_val) in enumerate(self.MODE_PRESETS):
            pill = QPushButton(label, objectName="ModePill")
            pill.setCursor(Qt.CursorShape.PointingHandCursor)
            pill.setProperty("active", "true" if idx == 3 else "false")
            pill.clicked.connect(
                lambda _checked=False, i=idx, d=details_val, s=state_val: self._on_mode_selected(i, d, s)
            )
            header_row.addWidget(pill)
            self._mode_pills.append(pill)

        layout.addLayout(header_row)

        # Row 2: Manual Details & State inputs + Dominant Engage Override + Companion Pause IPC & Reload Rules
        controls_row = QHBoxLayout()
        controls_row.setSpacing(8)

        self._details_input = QLineEdit()
        self._details_input.setPlaceholderText("Override details (e.g. Deep work session)")
        self._details_input.setFont(ui_font(12))
        self._details_input.returnPressed.connect(self._apply_manual)
        controls_row.addWidget(self._details_input, 2)

        self._state_input = QLineEdit()
        self._state_input.setPlaceholderText("Optional state (e.g. Do not disturb)")
        self._state_input.setFont(ui_font(12))
        self._state_input.returnPressed.connect(self._apply_manual)
        controls_row.addWidget(self._state_input, 2)

        self._manual_btn = QPushButton("Engage Override", objectName="PrimaryButton")
        self._manual_btn.setFont(ui_font(12, QFont.Weight.Bold))
        self._manual_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._manual_btn.clicked.connect(self._toggle_manual)
        controls_row.addWidget(self._manual_btn)

        self._pause_btn = QPushButton("Pause IPC", objectName="ToggleButton")
        self._pause_btn.setProperty("active", "false")
        self._pause_btn.setFont(ui_font(12, QFont.Weight.DemiBold))
        self._pause_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pause_btn.clicked.connect(self._on_pause_clicked)
        controls_row.addWidget(self._pause_btn)

        self._reload_btn = QPushButton("Reload Rules")
        self._reload_btn.setFont(ui_font(12, QFont.Weight.DemiBold))
        self._reload_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reload_btn.setToolTip("Reload config/rules.json from disk")
        self._reload_btn.clicked.connect(self.reload_requested.emit)
        controls_row.addWidget(self._reload_btn)

        layout.addLayout(controls_row)

    def _on_mode_selected(self, selected_idx: int, details_preset: str, state_preset: str) -> None:
        for idx, pill in enumerate(self._mode_pills):
            pill.setProperty("active", "true" if idx == selected_idx else "false")
            pill.style().unpolish(pill)
            pill.style().polish(pill)
        if details_preset:
            self._details_input.setText(details_preset)
            self._state_input.setText(state_preset)
            self._apply_manual()

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
        self._client_id = client_id

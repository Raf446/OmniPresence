"""Obsidian Glass SettingsCard container with Master ToggleSwitch and automatic child dimming."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ui import ui_font
from ui.components.toggle_switch import ToggleSwitch


class SettingsCard(QFrame):
    """Feature card with header title, subtitle, optional top-right Master ToggleSwitch, and dimmed body."""

    toggled = Signal(bool)

    def __init__(
        self,
        title: str,
        description: str,
        *,
        has_toggle: bool = True,
        checked: bool = True,
        badge_text: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent, objectName="SettingsCard")
        self._has_toggle = has_toggle

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(10)

        title_col = QVBoxLayout()
        title_col.setSpacing(3)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self._title_lbl = QLabel(title, objectName="CardTitle")
        self._title_lbl.setFont(ui_font(13, QFont.Weight.Bold))
        title_row.addWidget(self._title_lbl)

        if badge_text:
            badge = QLabel(badge_text, objectName="CardPillBadge")
            badge.setFont(ui_font(10, QFont.Weight.DemiBold))
            title_row.addWidget(badge)
        title_row.addStretch(1)
        title_col.addLayout(title_row)

        self._desc_lbl = QLabel(description, objectName="SectionCaption")
        self._desc_lbl.setFont(ui_font(11))
        self._desc_lbl.setWordWrap(True)
        title_col.addWidget(self._desc_lbl)

        header.addLayout(title_col, 1)

        self._switch: ToggleSwitch | None = None
        if has_toggle:
            self._switch = ToggleSwitch(checked=checked, parent=self)
            self._switch.toggled.connect(self._on_master_toggled)
            header.addWidget(self._switch, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)

        root.addLayout(header)

        divider = QFrame(objectName="CardDivider")
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFixedHeight(1)
        root.addWidget(divider)

        self._body = QWidget(self)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 2, 0, 0)
        self._body_layout.setSpacing(8)

        self._opacity = QGraphicsOpacityEffect(self._body)
        self._body.setGraphicsEffect(self._opacity)
        root.addWidget(self._body)

        self._apply_body_state(checked if has_toggle else True)

    @property
    def body_layout(self) -> QVBoxLayout:
        return self._body_layout

    @property
    def switch(self) -> ToggleSwitch | None:
        return self._switch

    def add_row(self, label_text: str, control: QWidget, caption: str = "") -> None:
        row = QHBoxLayout()
        row.setSpacing(12)
        lbl_col = QVBoxLayout()
        lbl_col.setSpacing(1)
        lbl = QLabel(label_text, objectName="SettingRowLabel")
        lbl.setFont(ui_font(12, QFont.Weight.DemiBold))
        lbl_col.addWidget(lbl)
        if caption:
            cap = QLabel(caption, objectName="SectionCaption")
            cap.setFont(ui_font(10))
            lbl_col.addWidget(cap)
        row.addLayout(lbl_col, 1)
        row.addWidget(control, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._body_layout.addLayout(row)

    def add_block(self, widget: QWidget) -> None:
        """Append a full-width custom widget block into the card body."""
        self._body_layout.addWidget(widget)

    def isChecked(self) -> bool:  # noqa: N802
        return self._switch.isChecked() if self._switch is not None else True

    def setChecked(self, checked: bool, *, animate: bool = False) -> None:  # noqa: N802
        if self._switch is not None:
            self._switch.blockSignals(True)
            self._switch.setChecked(checked, animate=animate)
            self._switch.blockSignals(False)
        self._apply_body_state(checked)

    def _on_master_toggled(self, checked: bool) -> None:
        self._apply_body_state(checked)
        self.toggled.emit(checked)

    def _apply_body_state(self, enabled: bool) -> None:
        self._body.setEnabled(bool(enabled))
        self._opacity.setOpacity(1.0 if enabled else 0.35)
        self.setProperty("active", "true" if enabled else "false")
        self.style().unpolish(self)
        self.style().polish(self)

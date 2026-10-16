"""Interactive rule table displaying priority tiers, matching triggers, and live arbitration state."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core.priority_engine import Resolution, RuleBook
from ui import ACCENT, DIM, MUTED, SUCCESS, TEXT, WARN, mono_font, ui_font


class RuleListWidget(QFrame):
    rule_toggled = Signal(str, bool)

    def __init__(self, rulebook: RuleBook, parent=None) -> None:
        super().__init__(parent, objectName="PanelCard")
        self._book = rulebook
        self._updating = False
        self._winner_id: str | None = None
        self._live_ids: frozenset[str] = frozenset()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("Context Detection Rules", objectName="SectionHeading")
        title.setFont(ui_font(13, QFont.Weight.DemiBold))
        header.addWidget(title)
        header.addStretch(1)
        self._count_label = QLabel("", objectName="SectionCaption")
        self._count_label.setFont(mono_font(11))
        header.addWidget(self._count_label)
        layout.addLayout(header)

        self._error_banner = QLabel("", objectName="RuleErrorBanner")
        self._error_banner.setWordWrap(True)
        self._error_banner.setFont(ui_font(12))
        self._error_banner.hide()
        layout.addWidget(self._error_banner)

        self._table = QTableWidget(0, 5, objectName="RuleTable")
        self._table.setHorizontalHeaderLabels(["On", "Tier", "Rule", "Trigger Scope", "State"])
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setShowGrid(False)

        h_header = self._table.horizontalHeader()
        h_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(0, 42)
        h_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(1, 86)
        h_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        h_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        h_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(4, 96)

        self._table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self._table, 1)

        self.set_rulebook(rulebook, "")

    def set_rulebook(self, rulebook: RuleBook, error: str = "") -> None:
        self._book = rulebook
        if error:
            self._error_banner.setText(f"Rule reload error: {error}")
            self._error_banner.show()
        else:
            self._error_banner.hide()
        self._populate()
        self._refresh_states()

    def set_resolution(self, resolution: Resolution | None) -> None:
        if resolution is None:
            self._winner_id = None
            self._live_ids = frozenset()
        else:
            self._winner_id = resolution.rule.id if resolution.rule else None
            self._live_ids = resolution.live_rule_ids
        self._refresh_states()

    def _populate(self) -> None:
        self._updating = True
        rules = self._book.rules
        self._table.setRowCount(len(rules))
        enabled_count = 0

        for row, rule in enumerate(rules):
            if rule.enabled:
                enabled_count += 1

            check_item = QTableWidgetItem()
            check_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            check_item.setCheckState(
                Qt.CheckState.Checked if rule.enabled else Qt.CheckState.Unchecked
            )
            check_item.setData(Qt.ItemDataRole.UserRole, rule.id)
            self._table.setItem(row, 0, check_item)

            tier_item = QTableWidgetItem(f"P{int(rule.tier)} {rule.tier.label}")
            tier_item.setFont(mono_font(11, QFont.Weight.DemiBold))
            tier_item.setForeground(QColor(TEXT if rule.enabled else DIM))
            self._table.setItem(row, 1, tier_item)

            name_item = QTableWidgetItem(rule.name)
            name_item.setFont(ui_font(12, QFont.Weight.DemiBold))
            name_item.setToolTip(f"id: {rule.id}")
            name_item.setForeground(QColor(TEXT if rule.enabled else DIM))
            self._table.setItem(row, 2, name_item)

            summary_item = QTableWidgetItem(rule.summary)
            summary_item.setFont(ui_font(12))
            summary_item.setForeground(QColor(MUTED if rule.enabled else DIM))
            summary_item.setToolTip(rule.summary)
            self._table.setItem(row, 3, summary_item)

            state_item = QTableWidgetItem()
            state_item.setFont(mono_font(11, QFont.Weight.DemiBold))
            state_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 4, state_item)
            self._table.setRowHeight(row, 32)

        self._count_label.setText(f"{enabled_count}/{len(rules)} enabled")
        self._updating = False

    def _refresh_states(self) -> None:
        self._updating = True
        for row, rule in enumerate(self._book.rules):
            state_item = self._table.item(row, 4)
            if state_item is None:
                continue
            if not rule.enabled:
                state_item.setText("DISABLED")
                state_item.setForeground(QColor(DIM))
            elif rule.id == self._winner_id:
                state_item.setText("ACTIVE")
                state_item.setForeground(QColor(SUCCESS))
            elif rule.id in self._live_ids:
                state_item.setText("STANDBY")
                state_item.setForeground(QColor(WARN))
            else:
                state_item.setText("IDLE")
                state_item.setForeground(QColor(MUTED))

            for col in (1, 2, 3):
                cell = self._table.item(row, col)
                if cell is None:
                    continue
                if rule.id == self._winner_id:
                    cell.setForeground(QColor(ACCENT if col == 1 else TEXT))
                elif not rule.enabled:
                    cell.setForeground(QColor(DIM))
                else:
                    cell.setForeground(QColor(MUTED if col == 3 else TEXT))
        self._updating = False

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or item.column() != 0:
            return
        rule_id = item.data(Qt.ItemDataRole.UserRole)
        if rule_id:
            enabled = item.checkState() == Qt.CheckState.Checked
            self.rule_toggled.emit(str(rule_id), enabled)

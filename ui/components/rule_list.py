"""Interactive rule table with custom-painted pill badges for Priority Tiers (P1-P6) and live arbitration state."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core.priority_engine import Resolution, RuleBook, Tier
from ui import DIM, MUTED, SUCCESS, TEXT, TIER_COLORS, WARN, mono_font, tier_color, ui_font

ROLE_TIER_INT = Qt.ItemDataRole.UserRole + 1
ROLE_RULE_ENABLED = Qt.ItemDataRole.UserRole + 2
ROLE_STATE_CODE = Qt.ItemDataRole.UserRole + 3


class PillBadgeDelegate(QStyledItemDelegate):
    """Renders rounded color-coded pill badges in the Tier (col 1) and State (col 4) columns."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.pulse_phase = 0.0

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        col = index.column()
        if col not in (1, 4):
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Draw row selection / hover background if active
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor(41, 121, 255, 46))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(option.rect, QColor(27, 33, 44, 210))

        rect = QRectF(option.rect).adjusted(6, 5, -6, -5)
        enabled = bool(index.data(ROLE_RULE_ENABLED))
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")

        if col == 1:
            tier_val = int(index.data(ROLE_TIER_INT) or 6)
            base = tier_color(tier_val)
            pulse = 0.5 + 0.5 * math.sin(self.pulse_phase)
            fill = QColor(base)
            fill.setAlpha(44 if enabled else 18)
            border = QColor(base)
            border.setAlpha(185 if enabled else 65)
            ink = QColor(base).lighter(135) if enabled else QColor(DIM)

            painter.setPen(QPen(border, 1.1))
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 9, 9)

            # Left color dot inside the tier pill (pulsating for P2 Compiler Active)
            dot_pt = QPointF(rect.left() + 10, rect.center().y())
            dot_col = QColor(base)
            if tier_val == int(Tier.BUILD) and enabled:
                dot_col.setAlpha(int(120 + 135 * pulse))
            else:
                dot_col.setAlpha(235 if enabled else 90)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(dot_col)
            painter.drawEllipse(dot_pt, 3.2, 3.2)

            painter.setPen(ink)
            painter.setFont(mono_font(10, QFont.Weight.Bold))
            text_rect = QRectF(rect.left() + 18, rect.top(), rect.width() - 22, rect.height())
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        else:
            state_code = str(index.data(ROLE_STATE_CODE) or "IDLE")
            palette_map = {
                "ACTIVE": QColor(SUCCESS),
                "STANDBY": QColor(WARN),
                "IDLE": QColor(TIER_COLORS[6]),
                "DISABLED": QColor(DIM),
            }
            base = palette_map.get(state_code, QColor(MUTED))
            fill = QColor(base)
            fill.setAlpha(42 if state_code in ("ACTIVE", "STANDBY") else 20)
            border = QColor(base)
            border.setAlpha(170 if state_code in ("ACTIVE", "STANDBY") else 75)
            ink = QColor(base).lighter(130) if state_code != "DISABLED" else QColor(DIM)

            painter.setPen(QPen(border, 1.0))
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 9, 9)

            painter.setPen(ink)
            painter.setFont(mono_font(10, QFont.Weight.Bold))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

        painter.restore()


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
        self._table.setMouseTracking(True)
        self._table.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setShowGrid(False)

        self._delegate = PillBadgeDelegate(self._table)
        self._table.setItemDelegate(self._delegate)

        h_header = self._table.horizontalHeader()
        h_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(0, 42)
        h_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(1, 102)
        h_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        h_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        h_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(4, 100)

        self._table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self._table, 1)

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(80)
        self._pulse_timer.timeout.connect(self._tick_pulse)
        self._pulse_timer.start()

        self.set_rulebook(rulebook, "")

    def _tick_pulse(self) -> None:
        self._delegate.pulse_phase = (self._delegate.pulse_phase + 0.22) % (math.pi * 2)
        self._table.viewport().update()

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
            tier_item.setData(ROLE_TIER_INT, int(rule.tier))
            tier_item.setData(ROLE_RULE_ENABLED, rule.enabled)
            tier_item.setToolTip(f"Priority P{int(rule.tier)} ({rule.tier.label}) \u2014 Color {TIER_COLORS.get(int(rule.tier), '')}")
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
            state_item.setData(ROLE_RULE_ENABLED, rule.enabled)
            self._table.setItem(row, 4, state_item)
            self._table.setRowHeight(row, 34)

        self._count_label.setText(f"{enabled_count}/{len(rules)} enabled")
        self._updating = False

    def _refresh_states(self) -> None:
        self._updating = True
        for row, rule in enumerate(self._book.rules):
            tier_item = self._table.item(row, 1)
            if tier_item is not None:
                tier_item.setData(ROLE_RULE_ENABLED, rule.enabled)

            state_item = self._table.item(row, 4)
            if state_item is None:
                continue
            state_item.setData(ROLE_RULE_ENABLED, rule.enabled)
            if not rule.enabled:
                state_code = "DISABLED"
            elif rule.id == self._winner_id:
                state_code = "ACTIVE"
            elif rule.id in self._live_ids:
                state_code = "STANDBY"
            else:
                state_code = "IDLE"
            state_item.setText(state_code)
            state_item.setData(ROLE_STATE_CODE, state_code)

            for col in (2, 3):
                cell = self._table.item(row, col)
                if cell is None:
                    continue
                if rule.id == self._winner_id:
                    cell.setForeground(QColor(TEXT))
                elif not rule.enabled:
                    cell.setForeground(QColor(DIM))
                else:
                    cell.setForeground(QColor(MUTED if col == 3 else TEXT))
        self._updating = False
        self._table.viewport().update()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or item.column() != 0:
            return
        rule_id = item.data(Qt.ItemDataRole.UserRole)
        if rule_id:
            enabled = item.checkState() == Qt.CheckState.Checked
            self.rule_toggled.emit(str(rule_id), enabled)

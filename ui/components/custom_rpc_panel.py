"""Full PreMiD-style Custom Rich Presence Studio (Tier P1 Override) with preset persistence and URL/button validation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from core.priority_engine import CustomRPCConfig
from ui import mono_font, ui_font


class CustomRPCPanel(QFrame):
    """Interactive studio for configuring full Custom Rich Presence (Tier P1) and managing JSON presets."""

    custom_rpc_submitted = Signal(bool, object)  # (enabled, CustomRPCConfig)

    def __init__(self, presets_path: Path, default_client_id: str = "", parent=None) -> None:
        super().__init__(parent, objectName="PanelCard")
        self._presets_path = presets_path
        self._default_client_id = default_client_id
        self._active = False
        self._presets: dict[str, dict[str, Any]] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # Header + Preset Toolbar
        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("Custom RPC Studio (PreMiD-Style Tier P1)", objectName="SectionHeading")
        title.setFont(ui_font(12, QFont.Weight.DemiBold))
        header.addWidget(title)

        caption = QLabel("Full manual override with custom images, tooltips, buttons & presets", objectName="SectionCaption")
        caption.setFont(ui_font(11))
        header.addWidget(caption)
        header.addStretch(1)

        preset_lbl = QLabel("Preset:", objectName="SectionCaption")
        preset_lbl.setFont(ui_font(11, QFont.Weight.DemiBold))
        header.addWidget(preset_lbl)

        self._preset_combo = QComboBox(objectName="PresetCombo")
        self._preset_combo.setMinimumWidth(185)
        self._preset_combo.setFont(ui_font(11))
        self._preset_combo.activated.connect(self._on_preset_selected)
        header.addWidget(self._preset_combo)

        self._save_preset_btn = QPushButton("Save Preset")
        self._save_preset_btn.setFont(ui_font(11, QFont.Weight.DemiBold))
        self._save_preset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_preset_btn.clicked.connect(self._save_preset_dialog)
        header.addWidget(self._save_preset_btn)

        self._del_preset_btn = QPushButton("Delete")
        self._del_preset_btn.setFont(ui_font(11))
        self._del_preset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._del_preset_btn.clicked.connect(self._delete_current_preset)
        header.addWidget(self._del_preset_btn)

        root.addLayout(header)

        # Form Grid
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(7)

        # Row 0: App ID + Timestamp Mode + Duration
        grid.addWidget(self._make_label("Application ID"), 0, 0)
        self._app_id = QLineEdit()
        self._app_id.setFont(mono_font(11))
        self._app_id.setPlaceholderText(
            f"Optional custom App ID (default: {default_client_id or 'from settings'})"
        )
        grid.addWidget(self._app_id, 0, 1)

        grid.addWidget(self._make_label("Timestamp"), 0, 2)
        ts_row = QHBoxLayout()
        ts_row.setContentsMargins(0, 0, 0, 0)
        ts_row.setSpacing(6)
        self._ts_combo = QComboBox()
        self._ts_combo.setFont(ui_font(11))
        self._ts_combo.addItem("Elapsed (Now)", "elapsed")
        self._ts_combo.addItem("None", "none")
        self._ts_combo.addItem("Custom Duration", "custom")
        self._ts_combo.currentIndexChanged.connect(self._on_ts_mode_changed)
        ts_row.addWidget(self._ts_combo, 1)

        self._duration_spin = QSpinBox()
        self._duration_spin.setFont(mono_font(11))
        self._duration_spin.setRange(1, 1440)
        self._duration_spin.setValue(60)
        self._duration_spin.setSuffix(" min")
        self._duration_spin.setToolTip("Custom countdown / duration in minutes")
        self._duration_spin.setEnabled(False)
        ts_row.addWidget(self._duration_spin, 0)
        grid.addLayout(ts_row, 0, 3)

        # Row 1: Details + State
        grid.addWidget(self._make_label("Details (Row 1)"), 1, 0)
        self._details = QLineEdit()
        self._details.setFont(ui_font(11))
        self._details.setPlaceholderText("e.g. Building OmniPresence v2.0")
        grid.addWidget(self._details, 1, 1)

        grid.addWidget(self._make_label("State (Row 2)"), 1, 2)
        self._state = QLineEdit()
        self._state.setFont(ui_font(11))
        self._state.setPlaceholderText("e.g. Autonomous Multi-Context Discord RPC")
        grid.addWidget(self._state, 1, 3)

        # Row 2: Large Image URL/Key + Large Tooltip
        grid.addWidget(self._make_label("Large Image"), 2, 0)
        self._large_image = QLineEdit()
        self._large_image.setFont(mono_font(11))
        self._large_image.setPlaceholderText("https://... or Discord Asset Key (default: op_manual)")
        grid.addWidget(self._large_image, 2, 1)

        grid.addWidget(self._make_label("Large Tooltip"), 2, 2)
        self._large_text = QLineEdit()
        self._large_text.setFont(ui_font(11))
        self._large_text.setPlaceholderText("Hover text for large image")
        grid.addWidget(self._large_text, 2, 3)

        # Row 3: Small Image URL/Key + Small Tooltip
        grid.addWidget(self._make_label("Small Image"), 3, 0)
        self._small_image = QLineEdit()
        self._small_image.setFont(mono_font(11))
        self._small_image.setPlaceholderText("https://... or Discord Asset Key (optional)")
        grid.addWidget(self._small_image, 3, 1)

        grid.addWidget(self._make_label("Small Tooltip"), 3, 2)
        self._small_text = QLineEdit()
        self._small_text.setFont(ui_font(11))
        self._small_text.setPlaceholderText("Hover text for small badge")
        grid.addWidget(self._small_text, 3, 3)

        # Row 4: Button 1 Label + Button 1 URL
        grid.addWidget(self._make_label("Button 1 Label"), 4, 0)
        self._btn1_label = QLineEdit()
        self._btn1_label.setFont(ui_font(11))
        self._btn1_label.setMaxLength(32)
        self._btn1_label.setPlaceholderText("Max 32 chars (e.g. View Repository)")
        grid.addWidget(self._btn1_label, 4, 1)

        grid.addWidget(self._make_label("Button 1 URL"), 4, 2)
        self._btn1_url = QLineEdit()
        self._btn1_url.setFont(mono_font(11))
        self._btn1_url.setPlaceholderText("https://github.com/...")
        grid.addWidget(self._btn1_url, 4, 3)

        # Row 5: Button 2 Label + Button 2 URL
        grid.addWidget(self._make_label("Button 2 Label"), 5, 0)
        self._btn2_label = QLineEdit()
        self._btn2_label.setFont(ui_font(11))
        self._btn2_label.setMaxLength(32)
        self._btn2_label.setPlaceholderText("Max 32 chars (e.g. Watch Stream)")
        grid.addWidget(self._btn2_label, 5, 1)

        grid.addWidget(self._make_label("Button 2 URL"), 5, 2)
        self._btn2_url = QLineEdit()
        self._btn2_url.setFont(mono_font(11))
        self._btn2_url.setPlaceholderText("https://...")
        grid.addWidget(self._btn2_url, 5, 3)

        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        root.addLayout(grid)

        # Bottom Action Bar + Validation Status
        footer = QHBoxLayout()
        footer.setSpacing(10)

        self._validation_lbl = QLabel(
            "Ready \u00b7 Buttons require non-empty labels and valid https:// URLs.",
            objectName="SectionCaption",
        )
        self._validation_lbl.setFont(ui_font(11))
        footer.addWidget(self._validation_lbl, 1)

        self._update_btn = QPushButton("Push Live Update")
        self._update_btn.setFont(ui_font(11, QFont.Weight.DemiBold))
        self._update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_btn.setToolTip("Immediately push edited fields while Custom RPC is active")
        self._update_btn.setEnabled(False)
        self._update_btn.clicked.connect(self._push_live_update)
        footer.addWidget(self._update_btn)

        self._engage_btn = QPushButton("Engage Custom RPC", objectName="PrimaryButton")
        self._engage_btn.setFont(ui_font(12, QFont.Weight.DemiBold))
        self._engage_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._engage_btn.setMinimumWidth(175)
        self._engage_btn.clicked.connect(self._toggle_engage)
        footer.addWidget(self._engage_btn)

        root.addLayout(footer)

        self._load_presets_from_disk()

    @staticmethod
    def _make_label(text: str) -> QLabel:
        lbl = QLabel(text, objectName="SectionCaption")
        lbl.setFont(ui_font(11, QFont.Weight.DemiBold))
        return lbl

    def _on_ts_mode_changed(self, _index: int) -> None:
        mode = self._ts_combo.currentData()
        self._duration_spin.setEnabled(mode == "custom")

    def build_config(self) -> CustomRPCConfig:
        mode = str(self._ts_combo.currentData() or "elapsed")
        duration_min = int(self._duration_spin.value())
        return CustomRPCConfig(
            app_id=self._app_id.text().strip(),
            details=self._details.text().strip() or "Custom Presence",
            state=self._state.text().strip(),
            large_image=self._large_image.text().strip() or "op_manual",
            large_text=self._large_text.text().strip() or "OmniPresence Custom RPC",
            small_image=self._small_image.text().strip(),
            small_text=self._small_text.text().strip(),
            button1_label=self._btn1_label.text().strip(),
            button1_url=self._btn1_url.text().strip(),
            button2_label=self._btn2_label.text().strip(),
            button2_url=self._btn2_url.text().strip(),
            timestamp_mode=mode,
            duration_minutes=duration_min,
        )

    def apply_config_to_inputs(self, cfg: CustomRPCConfig) -> None:
        self._app_id.setText(cfg.app_id)
        self._details.setText(cfg.details)
        self._state.setText(cfg.state)
        self._large_image.setText(cfg.large_image)
        self._large_text.setText(cfg.large_text)
        self._small_image.setText(cfg.small_image)
        self._small_text.setText(cfg.small_text)
        self._btn1_label.setText(cfg.button1_label)
        self._btn1_url.setText(cfg.button1_url)
        self._btn2_label.setText(cfg.button2_label)
        self._btn2_url.setText(cfg.button2_url)

        idx = self._ts_combo.findData(cfg.timestamp_mode)
        if idx >= 0:
            self._ts_combo.setCurrentIndex(idx)
        self._duration_spin.setValue(max(1, int(cfg.duration_minutes)))
        self._duration_spin.setEnabled(cfg.timestamp_mode == "custom")

    def _validate_inputs(self) -> tuple[bool, str]:
        for idx, (lbl_w, url_w) in enumerate(
            ((self._btn1_label, self._btn1_url), (self._btn2_label, self._btn2_url)),
            start=1,
        ):
            lbl = lbl_w.text().strip()
            url = url_w.text().strip()
            if lbl and not url:
                return False, f"Button {idx} has a label but no URL (must start with https://)."
            if url and not lbl:
                return False, f"Button {idx} has a URL but its label is empty."
            if url and not (url.lower().startswith("https://") and len(url) > len("https://")):
                return False, f"Button {idx} URL must start with https:// (Discord RPC restriction)."

        app_id = self._app_id.text().strip()
        if app_id and not app_id.isdigit():
            return False, "Custom Application ID must contain digits only (or leave blank for default)."
        return True, "Validated \u00b7 Ready for 0.25s fast-lane IPC dispatch."

    def _toggle_engage(self) -> None:
        if self._active:
            self.custom_rpc_submitted.emit(False, self.build_config())
            return
        ok, msg = self._validate_inputs()
        self._set_validation_banner(ok, msg)
        if not ok:
            return
        self.custom_rpc_submitted.emit(True, self.build_config())

    def _push_live_update(self) -> None:
        ok, msg = self._validate_inputs()
        self._set_validation_banner(ok, msg)
        if not ok:
            return
        self.custom_rpc_submitted.emit(True, self.build_config())

    def _set_validation_banner(self, ok: bool, message: str) -> None:
        self._validation_lbl.setText(message)
        self._validation_lbl.setStyleSheet(
            "color: #94a0b8;" if ok else "color: #ff6b6b; font-weight: 600;"
        )

    def set_custom_rpc_state(self, active: bool, config: CustomRPCConfig) -> None:
        self._active = active
        self._update_btn.setEnabled(active)
        self._engage_btn.setText("Disengage Custom RPC" if active else "Engage Custom RPC")
        self._engage_btn.setProperty("active", "true" if active else "false")
        self._engage_btn.style().unpolish(self._engage_btn)
        self._engage_btn.style().polish(self._engage_btn)
        if active:
            self.apply_config_to_inputs(config)
            self._set_validation_banner(True, "Custom RPC Active (Tier P1) \u00b7 Broadcasting live to Discord.")
        else:
            self._set_validation_banner(True, "Standby \u00b7 Auto context detection rules (P2\u2013P6) in control.")

    # --- Preset Persistence (config/custom_presets.json) ---

    def _load_presets_from_disk(self) -> None:
        self._presets.clear()
        try:
            if self._presets_path.exists():
                raw = json.loads(self._presets_path.read_text(encoding="utf-8"))
                presets = raw.get("presets", raw) if isinstance(raw, dict) else {}
                if isinstance(presets, dict):
                    for name, data in presets.items():
                        if isinstance(data, dict):
                            self._presets[str(name)] = data
        except Exception:
            pass
        self._refresh_preset_combo()
        if self._presets:
            first_name = next(iter(self._presets))
            self._load_named_preset(first_name)

    def _save_presets_to_disk(self) -> None:
        self._presets_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"presets": self._presets}
        self._presets_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _refresh_preset_combo(self, select_name: str | None = None) -> None:
        self._preset_combo.blockSignals(True)
        self._preset_combo.clear()
        for name in self._presets:
            self._preset_combo.addItem(name)
        if select_name and select_name in self._presets:
            idx = self._preset_combo.findText(select_name)
            if idx >= 0:
                self._preset_combo.setCurrentIndex(idx)
        self._preset_combo.blockSignals(False)
        self._del_preset_btn.setEnabled(bool(self._presets))

    def _on_preset_selected(self, index: int) -> None:
        name = self._preset_combo.itemText(index)
        if name:
            self._load_named_preset(name)

    def _load_named_preset(self, name: str) -> None:
        data = self._presets.get(name)
        if not data:
            return
        cfg = CustomRPCConfig.from_dict(data)
        self.apply_config_to_inputs(cfg)
        self._set_validation_banner(True, f"Loaded preset '{name}'.")
        if self._active:
            self._push_live_update()

    def _save_preset_dialog(self) -> None:
        ok, msg = self._validate_inputs()
        if not ok:
            self._set_validation_banner(False, msg)
            return
        current_name = self._preset_combo.currentText().strip() or "My Custom Preset"
        name, accepted = QInputDialog.getText(
            self,
            "Save Custom RPC Preset",
            "Preset Name:",
            QLineEdit.EchoMode.Normal,
            current_name,
        )
        clean_name = name.strip()
        if not accepted or not clean_name:
            return
        cfg = self.build_config()
        self._presets[clean_name] = cfg.to_dict()
        self._save_presets_to_disk()
        self._refresh_preset_combo(select_name=clean_name)
        self._set_validation_banner(True, f"Saved preset '{clean_name}' to {self._presets_path.name}.")

    def _delete_current_preset(self) -> None:
        name = self._preset_combo.currentText().strip()
        if not name or name not in self._presets:
            return
        self._presets.pop(name, None)
        self._save_presets_to_disk()
        self._refresh_preset_combo()
        self._set_validation_banner(True, f"Deleted preset '{name}'.")

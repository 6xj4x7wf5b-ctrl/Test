"""Chinese settings and alarms UI; every database operation is asynchronous."""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


class SettingsPage(QWidget):
    sound_changed = Signal(bool)

    def __init__(self, repository, controller, parent=None):
        super().__init__(parent)
        self.repo, self.controller = repository, controller
        self.sound_enabled = True
        self._devices, self._calibrations, self._thresholds, self._alarms = {}, {}, {}, []
        self._request_generation = 0
        self._build_ui()
        controller.metadata_changed.connect(self._metadata_changed)
        controller.alarms_changed.connect(self._alarms_changed)
        repository.ready.connect(self.reload_settings)
        self._metadata_changed(controller.metadata)
        if repository.thread.isRunning():
            self.reload_settings()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        title = QLabel("参数设置与报警")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        device_row = QHBoxLayout()
        device_row.addWidget(QLabel("当前设备"))
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(240)
        self.device_combo.setMaximumWidth(420)
        self.device_combo.currentIndexChanged.connect(self._device_changed)
        device_row.addWidget(self.device_combo)
        device_row.addStretch()
        layout.addLayout(device_row)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        self.message = QLabel("所有修正仅用于后续采集，历史记录保留采集时的原值与修正值。")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)

        collection = QWidget()
        collection_layout = QVBoxLayout(collection)
        group = QGroupBox("采集与历史")
        form = QFormLayout(group)
        self.poll_seconds = QSpinBox()
        self.poll_seconds.setRange(1, 86_400)
        self.poll_seconds.setSuffix(" 秒")
        self.poll_seconds.setValue(5)
        self.history_seconds = QSpinBox()
        self.history_seconds.setRange(1, 31_536_000)
        self.history_seconds.setSuffix(" 秒")
        self.history_seconds.setValue(30)
        self.poll_seconds.setMaximumWidth(150)
        self.history_seconds.setMaximumWidth(170)
        form.addRow("实时采集周期", self.poll_seconds)
        form.addRow("历史保存间隔", self.history_seconds)
        intervals = QPushButton("应用采集与保存周期")
        intervals.setMaximumWidth(180)
        intervals.clicked.connect(self._save_intervals)
        form.addRow(intervals)
        collection_layout.addWidget(group)
        note = QLabel("实时采集与历史保存分开执行。历史记录永久保留，只有手动清理才会删除；保存频率不会快于实际完成采样的频率。")
        note.setWordWrap(True)
        collection_layout.addWidget(note)
        self.sound_box = QCheckBox("开启报警声音（仅当前、未确认的报警发声）")
        self.sound_box.setChecked(True)
        self.sound_box.toggled.connect(self._save_sound)
        collection_layout.addWidget(self.sound_box)
        collection_layout.addStretch()
        tabs.addTab(collection, "采集与声音")

        calibration = QWidget()
        cal_layout = QVBoxLayout(calibration)
        cal_form = QFormLayout()
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("粮温测点", "grain")
        self.kind_combo.addItem("仓温仓湿", "ambient")
        self.kind_combo.currentIndexChanged.connect(self._kind_changed)
        self.point_spin = QSpinBox()
        self.point_spin.setRange(1, 165)
        self.point_spin.valueChanged.connect(self._fill_calibration)
        self.temp_offset = QDoubleSpinBox()
        self.temp_offset.setRange(-50, 50)
        self.temp_offset.setDecimals(2)
        self.temp_offset.setSingleStep(0.1)
        self.temp_offset.setSuffix(" ℃")
        self.humidity_offset = QDoubleSpinBox()
        self.humidity_offset.setRange(-100, 100)
        self.humidity_offset.setDecimals(2)
        self.humidity_offset.setSingleStep(0.1)
        self.humidity_offset.setSuffix(" %RH")
        for widget in (self.kind_combo, self.point_spin, self.temp_offset, self.humidity_offset):
            widget.setMaximumWidth(190)
        self.batch_box = QCheckBox("应用到当前设备的同类全部测点")
        self.batch_box.toggled.connect(lambda checked: self.point_spin.setEnabled(not checked and bool(self.device_combo.currentData())))
        cal_form.addRow("测点类型", self.kind_combo)
        cal_form.addRow("测点编号", self.point_spin)
        cal_form.addRow("温度加减修正", self.temp_offset)
        cal_form.addRow("湿度加减修正", self.humidity_offset)
        cal_form.addRow(self.batch_box)
        self.save_calibration_button = QPushButton("保存微调参数")
        self.save_calibration_button.setMaximumWidth(150)
        self.save_calibration_button.clicked.connect(self._save_calibration)
        cal_form.addRow(self.save_calibration_button)
        cal_layout.addLayout(cal_form)
        self.calibration_version = QLabel("尚未选择设备")
        cal_layout.addWidget(self.calibration_version)
        explanation = QLabel("修正值 = 原始值 + 微调值。保存后仅对之后采集生效；历史数据不会重新计算。批量操作在一个事务中完成，粮温测点不进行湿度修正。")
        explanation.setWordWrap(True)
        cal_layout.addWidget(explanation)
        cal_layout.addStretch()
        tabs.addTab(calibration, "测点微调")

        alarms = QWidget()
        alarms_layout = QVBoxLayout(alarms)
        threshold_group = QGroupBox("当前设备报警阈值")
        threshold_form = QFormLayout(threshold_group)
        self.threshold_kind = QComboBox()
        self.threshold_kind.addItem("粮温测点", "grain")
        self.threshold_kind.addItem("仓温仓湿", "ambient")
        self.threshold_kind.currentIndexChanged.connect(self._fill_thresholds)
        self.threshold_kind.setMaximumWidth(190)
        threshold_form.addRow("适用类型", self.threshold_kind)
        self.threshold_inputs = {}
        for field, label in (("temp_low", "温度下限（℃）"), ("temp_high", "温度上限（℃）"),
                             ("humidity_low", "湿度下限（%RH）"), ("humidity_high", "湿度上限（%RH）")):
            editor = QLineEdit()
            editor.setPlaceholderText("留空表示不启用")
            editor.setMaximumWidth(190)
            self.threshold_inputs[field] = editor
            threshold_form.addRow(label, editor)
        self.save_threshold_button = QPushButton("保存阈值")
        self.save_threshold_button.setMaximumWidth(150)
        self.save_threshold_button.clicked.connect(self._save_threshold)
        threshold_form.addRow(self.save_threshold_button)
        alarms_layout.addWidget(threshold_group)
        alarm_toolbar = QHBoxLayout()
        ack = QPushButton("确认 / 消音所选报警")
        ack.clicked.connect(self._ack_selected)
        ack_all = QPushButton("全部消音")
        ack_all.clicked.connect(lambda: self._acknowledge(None))
        refresh = QPushButton("刷新报警")
        refresh.clicked.connect(self.controller.refresh_alarms)
        for button in (ack, ack_all, refresh):
            alarm_toolbar.addWidget(button)
        alarm_toolbar.addStretch()
        alarms_layout.addLayout(alarm_toolbar)
        self.alarm_table = QTableWidget(0, 9)
        self.alarm_table.setHorizontalHeaderLabels(["时间", "设备", "来源", "测点", "指标", "测量值", "阈值", "状态", "确认 / 消音"])
        self.alarm_table.verticalHeader().setDefaultSectionSize(28)
        self.alarm_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.alarm_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.alarm_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.alarm_table.horizontalHeader().setStretchLastSection(True)
        alarms_layout.addWidget(self.alarm_table, 1)
        tabs.addTab(alarms, "阈值与报警")

    def reload_settings(self):
        self.repo.call("get_setting", "sound_enabled", True, callback=self._sound_loaded, on_error=self._error)
        self.repo.call("get_setting", "collection", {}, callback=self._intervals_loaded, on_error=self._error)
        self._device_changed()
        self.controller.refresh_alarms()

    refresh = reload_settings

    def _sound_loaded(self, value):
        self.sound_enabled = value if isinstance(value, bool) else True
        self.sound_box.blockSignals(True)
        self.sound_box.setChecked(self.sound_enabled)
        self.sound_box.blockSignals(False)
        self.sound_changed.emit(self.sound_enabled)

    def _intervals_loaded(self, value):
        if not isinstance(value, dict):
            return
        try:
            self.poll_seconds.setValue(int(value.get("poll_seconds", 5)))
            self.history_seconds.setValue(int(value.get("history_seconds", 30)))
            self.controller.set_intervals(self.poll_seconds.value(), self.history_seconds.value())
        except (TypeError, ValueError):
            self.message.setText("保存的采集周期无效，请重新设置。")

    def _save_intervals(self):
        self.controller.set_intervals(self.poll_seconds.value(), self.history_seconds.value())
        self.message.setText("采集与历史保存周期已应用；已有历史记录保留。")

    def _save_sound(self, checked):
        def saved(_):
            self.sound_enabled = checked
            self.sound_changed.emit(checked)
            self.message.setText("报警声音已开启。" if checked else "报警声音已关闭，报警记录继续保存。")
        def failed(message):
            self.sound_box.blockSignals(True)
            self.sound_box.setChecked(self.sound_enabled)
            self.sound_box.blockSignals(False)
            self._error(message)
        self.repo.call("set_setting", "sound_enabled", bool(checked), callback=saved, on_error=failed)

    def _metadata_changed(self, metadata):
        previous = self.device_combo.currentData()
        self._devices = {device["id"]: device for device in metadata.get("devices", [])}
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for device in self._devices.values():
            source = "模拟" if device.get("source") == "simulator" else "串口"
            self.device_combo.addItem(f"{device.get('warehouse_name', '')} / {device['name']}（{source}）", device["id"])
        index = self.device_combo.findData(previous)
        if index >= 0:
            self.device_combo.setCurrentIndex(index)
        self.device_combo.blockSignals(False)
        self._device_changed()

    def _device_changed(self, *_):
        self._request_generation += 1
        request = self._request_generation
        identifier = self.device_combo.currentData()
        self._calibrations, self._thresholds = {}, {}
        enabled = identifier is not None
        for widget in (self.kind_combo, self.threshold_kind, self.temp_offset, self.batch_box,
                       self.save_calibration_button, self.save_threshold_button):
            widget.setEnabled(enabled)
        self._kind_changed()
        self._fill_thresholds()
        if not enabled:
            self.calibration_version.setText("请先创建仓房、连接和设备。")
            return
        device = self._devices[identifier]
        self.calibration_version.setText(f"设备当前修正版本：{device.get('calibration_version', 0)}")
        def calibrations(rows):
            if request == self._request_generation:
                self._calibrations = {(row["kind"], row["point"]): row for row in rows}
                self._fill_calibration()
        def thresholds(rows):
            if request == self._request_generation:
                self._thresholds = {row["kind"]: row for row in rows}
                self._fill_thresholds()
        def failed(message):
            if request == self._request_generation:
                self._error(message)
        self.repo.call("calibrations", identifier, callback=calibrations, on_error=failed)
        self.repo.call("thresholds", identifier, callback=thresholds, on_error=failed)

    def _kind_changed(self, *_):
        device = self._devices.get(self.device_combo.currentData())
        if device is None:
            self.point_spin.setEnabled(False)
            self.humidity_offset.setEnabled(False)
            return
        for combo in (self.kind_combo, self.threshold_kind):
            for index, field in ((0, "grain_count"), (1, "ambient_count")):
                combo.model().item(index).setEnabled(device[field] > 0)
            field = "grain_count" if combo.currentData() == "grain" else "ambient_count"
            if device[field] == 0:
                combo.setCurrentIndex(1 - combo.currentIndex())
        kind = self.kind_combo.currentData()
        self.point_spin.setMaximum(device["grain_count" if kind == "grain" else "ambient_count"])
        self.point_spin.setEnabled(not self.batch_box.isChecked())
        self.humidity_offset.setEnabled(kind == "ambient")
        self._fill_calibration()

    def _fill_calibration(self, *_):
        row = self._calibrations.get((self.kind_combo.currentData(), self.point_spin.value()), {})
        self.temp_offset.setValue(row.get("temp_offset", 0))
        self.humidity_offset.setValue(row.get("humidity_offset", 0) if self.kind_combo.currentData() == "ambient" else 0)

    def _save_calibration(self):
        identifier = self.device_combo.currentData()
        device = self._devices.get(identifier)
        if device is None:
            return
        kind = self.kind_combo.currentData()
        points = range(1, device["grain_count" if kind == "grain" else "ambient_count"] + 1) if self.batch_box.isChecked() else [self.point_spin.value()]
        rows = [dict(kind=kind, point=point, temp_offset=self.temp_offset.value(),
                     humidity_offset=self.humidity_offset.value() if kind == "ambient" else 0) for point in points]
        self.save_calibration_button.setEnabled(False)
        def saved(version):
            self.save_calibration_button.setEnabled(True)
            self.message.setText(f"微调参数已保存，共{len(rows)}个测点。仅后续采集生效，历史保持原值。")
            if identifier in self._devices:
                self._devices[identifier]["calibration_version"] = version
            self._device_changed()
        def failed(message):
            self.save_calibration_button.setEnabled(True)
            self._error(message)
        self.repo.call("save_calibrations", identifier, rows, callback=saved, on_error=failed)

    def _fill_thresholds(self, *_):
        enabled = self.device_combo.currentData() is not None
        kind = self.threshold_kind.currentData()
        row = self._thresholds.get(kind, {})
        for field, editor in self.threshold_inputs.items():
            editor.setEnabled(enabled and (not field.startswith("humidity") or kind == "ambient"))
            value = row.get(field) if editor.isEnabled() else None
            editor.setText("" if value is None else str(value))

    def _save_threshold(self):
        identifier = self.device_combo.currentData()
        if identifier is None:
            return
        try:
            values = {}
            for field, editor in self.threshold_inputs.items():
                text = editor.text().strip() if editor.isEnabled() else ""
                value = float(text) if text else None
                if value is not None and not math.isfinite(value):
                    raise ValueError("阈值必须是有限数字，或留空")
                values[field] = value
        except ValueError:
            self._error("阈值必须填写数字，留空表示不启用。")
            return
        self.save_threshold_button.setEnabled(False)
        def saved(_):
            self.save_threshold_button.setEnabled(True)
            self.message.setText("报警阈值已保存，将根据后续有效采样判断；异常或过期数据不触发阈值报警。")
            self._device_changed()
        def failed(message):
            self.save_threshold_button.setEnabled(True)
            self._error(message)
        self.repo.call("save_threshold", identifier, self.threshold_kind.currentData(), callback=saved, on_error=failed, **values)

    def _alarms_changed(self, rows):
        self._alarms = rows
        self.alarm_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            source = "模拟数据" if row["source"] == "simulator" else "串口设备"
            point = f"{'粮温' if row['kind'] == 'grain' else '仓温仓湿'} {row['point']}"
            metric = ("温度" if row["metric"] == "temp" else "湿度") + ("低于下限" if row["direction"] == "low" else "高于上限")
            values = [row["timestamp"], row.get("device_name", str(row["device_id"])), source, point, metric,
                      f"{row['value']:.2f}", f"{row['threshold']:.2f}", "当前报警" if row["active"] else "已恢复",
                      row.get("acknowledged_at") or "未确认"]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, row["id"])
                self.alarm_table.setItem(index, column, item)
        self.alarm_table.resizeColumnsToContents()

    def _ack_selected(self):
        selected = {index.row() for index in self.alarm_table.selectionModel().selectedRows()}
        identifiers = [self._alarms[index]["id"] for index in selected
                       if self._alarms[index]["active"] and not self._alarms[index].get("acknowledged_at")]
        if identifiers:
            self._acknowledge(identifiers)
        else:
            self.message.setText("请选择当前且未确认的报警；已恢复或已确认的记录保持不变。")

    def _acknowledge(self, identifiers):
        def saved(count):
            self.message.setText(f"已确认并消音{count}条当前报警，记录继续保留。恢复后再次越限会形成新的报警。")
            self.controller.refresh_alarms()
        self.repo.call("acknowledge_alarms", identifiers, callback=saved, on_error=self._error)

    def _error(self, message):
        self.message.setText(str(message))
        QMessageBox.warning(self, "操作未完成", str(message))

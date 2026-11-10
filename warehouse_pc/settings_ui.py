"""Chinese settings and alarms UI; every database operation is asynchronous."""
from __future__ import annotations

import math

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .filter_panel import FilterPanel
from .i18n import tr


class SettingsPage(QWidget):
    sound_changed = Signal(bool)
    tab_changed = Signal(int)

    def __init__(self, repository, controller, parent=None):
        super().__init__(parent)
        self.repo, self.controller = repository, controller
        self.sound_enabled = True
        self._devices, self._calibrations, self._thresholds, self._alarms = {}, {}, {}, []
        self._active_device = None
        self._calibration_point = ("grain", 1)
        self._threshold_type = "grain"
        self._calibration_baseline = (0.0, 0.0)
        self._threshold_baseline = {}
        self._request_generation = {"calibrations": 0, "thresholds": 0}
        self._loaded = {"calibrations": False, "thresholds": False}
        self._loading = set()
        self._saving = None
        self._interval_baseline = None
        self._collection_busy = self._sound_busy = self._sound_ready = False
        self._build_ui()
        controller.metadata_changed.connect(self._metadata_changed)
        controller.alarms_changed.connect(self._alarms_changed)
        repository.ready.connect(self.reload_settings)
        self._metadata_changed(controller.metadata)
        if repository.thread.isRunning():
            self.reload_settings()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        tabs = QTabWidget()
        self._tabs = tabs
        layout.addWidget(tabs, 1)
        feedback = QHBoxLayout()
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.hide()
        self.draft_label = QLabel(tr("有未保存的修改"))
        self.draft_label.setObjectName("muted")
        self.draft_label.hide()
        feedback.addWidget(self.message, 1)
        feedback.addWidget(self.draft_label, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(feedback)

        collection = QWidget()
        collection.setObjectName("settingsPanel")
        collection_layout = QVBoxLayout(collection)
        global_title = QLabel(tr("所有分机"))
        global_title.setObjectName("sectionTitle")
        collection_layout.addWidget(global_title)
        form = QFormLayout()
        self.poll_seconds = QSpinBox()
        self.poll_seconds.setRange(1, 86_400)
        self.poll_seconds.setSuffix(tr(" 秒"))
        self.poll_seconds.setValue(5)
        self.history_seconds = QSpinBox()
        self.history_seconds.setRange(1, 31_536_000)
        self.history_seconds.setSuffix(tr(" 秒"))
        self.history_seconds.setValue(30)
        self.poll_seconds.setMaximumWidth(150)
        self.history_seconds.setMaximumWidth(170)
        self.poll_seconds.setToolTip(tr("上位机请求数据的间隔。实际刷新速度取决于串口和分机。"))
        self.history_seconds.setToolTip(tr("控制历史入库频率，不影响实时显示。采集记录仅可手动清理。"))
        form.addRow(tr("采集周期"), self.poll_seconds)
        form.addRow(tr("历史保存间隔"), self.history_seconds)
        self.intervals_button = intervals = QPushButton(tr("保存设置"))
        intervals.setObjectName("primary")
        intervals.setMaximumWidth(180)
        intervals.clicked.connect(self._save_intervals)
        save_row = QHBoxLayout()
        save_row.addWidget(intervals)
        self.global_loading = QLabel()
        self.global_loading.setObjectName("muted")
        save_row.addWidget(self.global_loading)
        save_row.addStretch()
        form.addRow(save_row)
        collection_layout.addLayout(form)
        self.sound_box = QCheckBox(tr("报警声音"))
        self.sound_box.setToolTip(tr("当前且未确认的报警发声。关闭声音仍会保留报警记录。"))
        self.sound_box.setChecked(True)
        self.sound_box.toggled.connect(self._save_sound)
        collection_layout.addWidget(self.sound_box)
        collection_layout.addStretch()
        tabs.addTab(collection, tr("采集设置"))

        calibration = QWidget()
        calibration.setObjectName("settingsScopePage")
        cal_layout = QVBoxLayout(calibration)
        calibration_bar, self.device_combo, self.retry_device_button, self.device_loading = self._device_toolbar()
        cal_layout.addWidget(calibration_bar)
        cal_content = QFrame()
        cal_content.setObjectName("section")
        cal_content_layout = QVBoxLayout(cal_content)
        cal_content_layout.setContentsMargins(12, 10, 12, 12)
        cal_layout.addWidget(cal_content)
        self._cal_form = cal_form = QFormLayout()
        self.kind_combo = QComboBox()
        self.kind_combo.addItem(tr("粮温测点"), "grain")
        self.kind_combo.addItem(tr("仓温仓湿"), "ambient")
        self.kind_combo.currentIndexChanged.connect(self._kind_changed)
        self.point_combo = QComboBox()
        self.point_combo.currentIndexChanged.connect(self._kind_changed)
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
        self.kind_combo.setMaximumWidth(300)
        for widget in (self.point_combo, self.temp_offset, self.humidity_offset):
            widget.setMaximumWidth(190)
        self.batch_box = QCheckBox(tr("批量应用到同类测点"))
        self.batch_box.toggled.connect(self._update_enabled)
        cal_form.addRow(tr("测点类型"), self.kind_combo)
        cal_form.addRow(tr("测点编号"), self.point_combo)
        cal_form.addRow(tr("温度偏移"), self.temp_offset)
        cal_form.addRow(tr("湿度偏移"), self.humidity_offset)
        for offset in (self.temp_offset, self.humidity_offset):
            offset.setToolTip(tr("修正值等于原始值加偏移。仅影响以后采集，历史记录保持原值。"))
        cal_form.addRow(self.batch_box)
        self.save_calibration_button = QPushButton(tr("保存微调"))
        self.save_calibration_button.setObjectName("primary")
        self.save_calibration_button.setMaximumWidth(240)
        self.save_calibration_button.clicked.connect(self._save_calibration)
        cal_form.addRow(self.save_calibration_button)
        cal_content_layout.addLayout(cal_form)
        self.calibration_version = QLabel(tr("尚未选择设备"))
        self.calibration_version.setObjectName("muted")
        cal_content_layout.addWidget(self.calibration_version)
        cal_layout.addStretch()
        tabs.addTab(calibration, tr("温湿度微调"))

        alarms = QWidget()
        alarms.setObjectName("settingsScopePage")
        alarms_layout = QVBoxLayout(alarms)
        alarms_bar, self.alarm_device_combo, self.retry_alarm_device_button, self.alarm_device_loading = self._device_toolbar()
        alarms_layout.addWidget(alarms_bar)
        threshold_content = QFrame()
        threshold_content.setObjectName("section")
        threshold_content_layout = QVBoxLayout(threshold_content)
        threshold_content_layout.setContentsMargins(12, 10, 12, 12)
        alarms_layout.addWidget(threshold_content)
        thresholds_heading = QLabel(tr("报警阈值"))
        thresholds_heading.setObjectName("sectionTitle")
        threshold_content_layout.addWidget(thresholds_heading)
        self._threshold_form = threshold_form = QFormLayout()
        self.threshold_kind = QComboBox()
        self.threshold_kind.addItem(tr("粮温测点"), "grain")
        self.threshold_kind.addItem(tr("仓温仓湿"), "ambient")
        self.threshold_kind.currentIndexChanged.connect(self._threshold_changed)
        self.threshold_kind.setMaximumWidth(300)
        threshold_form.addRow(tr("测点类型"), self.threshold_kind)
        self.threshold_inputs = {}
        for field, label in (("temp_low", tr("温度下限（℃）")), ("temp_high", tr("温度上限（℃）")),
                             ("humidity_low", tr("湿度下限（%RH）")), ("humidity_high", tr("湿度上限（%RH）"))):
            editor = QLineEdit()
            editor.setPlaceholderText(tr("不设限"))
            editor.setMaximumWidth(190)
            self.threshold_inputs[field] = editor
            threshold_form.addRow(label, editor)
        self.save_threshold_button = QPushButton(tr("保存阈值"))
        self.save_threshold_button.setObjectName("primary")
        self.save_threshold_button.setMaximumWidth(150)
        self.save_threshold_button.clicked.connect(self._save_threshold)
        threshold_form.addRow(self.save_threshold_button)
        threshold_content_layout.addLayout(threshold_form)
        alarm_toolbar = QHBoxLayout()
        alarm_heading = QLabel(tr("全部分机报警"))
        alarm_heading.setObjectName("sectionTitle")
        alarm_toolbar.addWidget(alarm_heading, 1)
        ack = QPushButton(tr("确认所选"))
        ack.setToolTip(tr("确认并消音所选的当前报警"))
        ack.clicked.connect(self._ack_selected)
        ack_all = QPushButton(tr("全部消音"))
        ack_all.clicked.connect(lambda: self._acknowledge(None))
        refresh = QPushButton(tr("刷新"))
        refresh.clicked.connect(self.controller.refresh_alarms)
        for button in (ack, ack_all, refresh):
            button.setObjectName("secondary")
            alarm_toolbar.addWidget(button)
        alarms_layout.addLayout(alarm_toolbar)
        self.alarm_table = QTableWidget(0, 9)
        self.alarm_table.setHorizontalHeaderLabels([
            tr("时间"), tr("设备"), tr("来源"), tr("测点"), tr("指标"), tr("测量值"),
            tr("阈值"), tr("状态"), tr("确认 / 消音"),
        ])
        self.alarm_table.verticalHeader().setDefaultSectionSize(28)
        self.alarm_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.alarm_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.alarm_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.alarm_table.horizontalHeader().setStretchLastSection(True)
        alarms_layout.addWidget(self.alarm_table, 1)
        tabs.addTab(alarms, tr("报警记录"))
        self._device_combos = (self.device_combo, self.alarm_device_combo)
        self._device_retries = (self.retry_device_button, self.retry_alarm_device_button)
        self._device_loaders = (self.device_loading, self.alarm_device_loading)
        tabs.currentChanged.connect(self.tab_changed.emit)
        for widget in (self.poll_seconds, self.history_seconds, self.temp_offset, self.humidity_offset):
            widget.valueChanged.connect(self._update_draft_notice)
        for editor in self.threshold_inputs.values():
            editor.textChanged.connect(self._update_draft_notice)
        self._update_enabled()

    def _device_toolbar(self):
        panel = FilterPanel()
        combo = QComboBox()
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(12)
        combo.currentIndexChanged.connect(lambda index: self._device_changed(combo))
        panel.add_field(tr("分机"), combo)
        actions = QWidget()
        row = QHBoxLayout(actions)
        row.setContentsMargins(0, 0, 0, 0)
        loading = QLabel()
        loading.setObjectName("muted")
        row.addWidget(loading)
        retry = QPushButton(tr("重新读取"))
        retry.setObjectName("secondary")
        retry.setToolTip(tr("重新读取所选分机的微调和报警阈值"))
        retry.clicked.connect(self._reload_device_parameters)
        row.addWidget(retry)
        panel.add_action(actions)
        return panel, combo, retry, loading

    def current_tab(self):
        return self._tabs.currentIndex()

    def set_tab(self, index):
        self._tabs.setCurrentIndex(index)

    def reload_settings(self):
        self._load_globals()
        if not self._saving and not self._loading and not self._device_dirty():
            self._load_parameters()
        self.controller.refresh_alarms()

    refresh = reload_settings

    def _load_globals(self):
        if not self._collection_busy and not self._intervals_dirty():
            self._collection_busy = True
            self._interval_baseline = None
            self.repo.call("get_setting", "collection", {}, callback=self._intervals_loaded,
                           on_error=self._intervals_failed)
        if not self._sound_busy:
            self._sound_busy = True
            self._sound_ready = False
            self.repo.call("get_setting", "sound_enabled", True, callback=self._sound_loaded,
                           on_error=self._sound_failed)
        self._update_enabled()

    def _interval_values(self):
        return self.poll_seconds.value(), self.history_seconds.value()

    def _intervals_dirty(self):
        return self._interval_baseline is not None and self._interval_values() != self._interval_baseline

    def _intervals_loaded(self, value):
        if not isinstance(value, dict):
            self._intervals_failed(tr("全局采集设置格式无效"))
            return
        poll, history = value.get("poll_seconds", 5), value.get("history_seconds", 30)
        if (type(poll) is not int or not 1 <= poll <= self.poll_seconds.maximum()
                or type(history) is not int or not 1 <= history <= self.history_seconds.maximum()):
            self._intervals_failed(tr("全局采集设置超出允许范围"))
            return
        self._collection_busy = False
        self.poll_seconds.setValue(poll)
        self.history_seconds.setValue(history)
        self._interval_baseline = (poll, history)
        self._update_enabled()

    def _intervals_failed(self, message):
        self._collection_busy = False
        self._update_enabled()
        self._error(message)

    def _save_intervals(self):
        if self._interval_baseline is None or self._collection_busy:
            return
        poll, history = self._interval_values()
        self._collection_busy = True
        self._update_enabled()

        def saved(_):
            self._collection_busy = False
            self._interval_baseline = (poll, history)
            self.controller.set_intervals(poll, history, persist=False)
            self._update_enabled()
            self._show_message(tr("所有分机的采集与保存周期已应用，已有历史记录保留。"))

        self.repo.call("set_setting", "collection", {"poll_seconds": poll, "history_seconds": history},
                       callback=saved, on_error=self._intervals_failed)

    def _sound_loaded(self, value):
        if not isinstance(value, bool):
            self._sound_failed(tr("报警声音设置格式无效"))
            return
        self._sound_busy = False
        self._sound_ready = True
        self.sound_enabled = value
        with QSignalBlocker(self.sound_box):
            self.sound_box.setChecked(value)
        self.sound_changed.emit(value)
        self._update_enabled()

    def _sound_failed(self, message):
        self._sound_busy = False
        with QSignalBlocker(self.sound_box):
            self.sound_box.setChecked(self.sound_enabled)
        self._update_enabled()
        self._error(message)

    def _save_sound(self, checked):
        if not self._sound_ready or self._sound_busy:
            return
        self._sound_busy = True
        self._update_enabled()

        def saved(_):
            self._sound_loaded(bool(checked))
            self._show_message(tr("报警声音已开启。") if checked else tr("报警声音已关闭，报警记录继续保存。"))

        self.repo.call("set_setting", "sound_enabled", bool(checked), callback=saved, on_error=self._sound_failed)

    def _metadata_changed(self, metadata):
        previous = self._active_device
        self._devices = {device["id"]: dict(device) for device in metadata.get("devices", [])}
        identifier = previous if previous in self._devices else next(iter(self._devices), None)
        for combo in self._device_combos:
            with QSignalBlocker(combo):
                combo.clear()
                for device in self._devices.values():
                    source = tr("模拟") if device.get("source") == "simulator" else tr("串口")
                    combo.addItem(f"{device.get('warehouse_name', '')} / {device['name']}（{source}）", device["id"])
                combo.setCurrentIndex(combo.findData(identifier))
        if previous != identifier:
            self._select_device(identifier)
        elif not self._device_dirty() and not self._saving:
            self._sync_device_selectors()
            if self._loaded["calibrations"]:
                self._fill_calibration()
            if self._loaded["thresholds"]:
                self._fill_thresholds()
        self._update_enabled()

    def _device_changed(self, combo):
        identifier = combo.currentData()
        if identifier == self._active_device:
            return
        if self._saving or (self._device_dirty() and not self._confirm_discard()):
            identifier = self._active_device
        for selector in self._device_combos:
            with QSignalBlocker(selector):
                selector.setCurrentIndex(selector.findData(identifier))
        if identifier != self._active_device:
            self._select_device(identifier)

    def _select_device(self, identifier):
        self._active_device = identifier
        self._calibrations, self._thresholds = {}, {}
        self._loaded = dict.fromkeys(self._loaded, False)
        self._sync_device_selectors()
        self._load_parameters()

    def _points(self, kind):
        device = self._devices.get(self._active_device)
        return [point for key, point in self.controller.point_keys(device) if key == kind] if device else []

    def _sync_device_selectors(self, point=None):
        available = {kind: self._points(kind) for kind in ("grain", "ambient")}
        for combo in (self.kind_combo, self.threshold_kind):
            with QSignalBlocker(combo):
                for index, kind in enumerate(("grain", "ambient")):
                    combo.model().item(index).setEnabled(bool(available[kind]))
                if any(available.values()) and not available[combo.currentData()]:
                    combo.setCurrentIndex(1 - combo.currentIndex())
        previous = self.point_combo.currentData() if point is None else point
        with QSignalBlocker(self.point_combo):
            self.point_combo.clear()
            for point in available[self.kind_combo.currentData()]:
                self.point_combo.addItem(str(point), point)
            self.point_combo.setCurrentIndex(max(0, self.point_combo.findData(previous)))

    def _update_enabled(self, *_):
        for combo in self._device_combos:
            combo.setEnabled(bool(self._devices) and not self._saving)
        for retry in self._device_retries:
            retry.setEnabled(self._active_device is not None and not self._saving)
        calibration = self._loaded["calibrations"] and not self._saving and self.point_combo.count() > 0
        threshold = self._loaded["thresholds"] and not self._saving and self._points(self.threshold_kind.currentData())
        for widget in (self.kind_combo, self.temp_offset, self.batch_box, self.save_calibration_button):
            widget.setEnabled(bool(calibration))
        self.point_combo.setEnabled(bool(calibration and not self.batch_box.isChecked()))
        self.humidity_offset.setEnabled(bool(calibration and self.kind_combo.currentData() == "ambient"))
        self._cal_form.setRowVisible(self.humidity_offset, self.kind_combo.currentData() == "ambient")
        self.threshold_kind.setEnabled(bool(threshold))
        self.save_threshold_button.setEnabled(bool(threshold))
        for field, editor in self.threshold_inputs.items():
            applies = not field.startswith("humidity") or self.threshold_kind.currentData() == "ambient"
            editor.setEnabled(bool(threshold and applies))
            self._threshold_form.setRowVisible(editor, applies)
        global_ready = self._interval_baseline is not None and not self._collection_busy
        for widget in (self.poll_seconds, self.history_seconds, self.intervals_button):
            widget.setEnabled(global_ready)
        self.sound_box.setEnabled(self._sound_ready and not self._sound_busy)
        for loading in self._device_loaders:
            loading.setText(tr("正在保存…") if self._saving else tr("正在读取…"))
            loading.setVisible(bool(self._saving or self._loading))
        self.global_loading.setText(tr("正在读取…") if self._interval_baseline is None else tr("正在保存…"))
        self.global_loading.setVisible(self._collection_busy)
        self._update_draft_notice()

    def _update_draft_notice(self, *_):
        self.draft_label.setVisible(self._intervals_dirty() or self._device_dirty())

    def _show_message(self, text):
        self.message.setText(text)
        self.message.setVisible(bool(text))

    def _load_parameters(self, section=None):
        identifier = self._active_device
        sections = (section,) if section else ("calibrations", "thresholds")
        if identifier is None:
            self._loading.clear()
            for key in sections:
                self._request_generation[key] += 1
                self._loaded[key] = False
            self.calibration_version.setText(tr("请先创建仓房、连接和设备。"))
            self.calibration_version.show()
            self._fill_calibration()
            self._fill_thresholds()
            self._update_enabled()
            return
        for key in sections:
            self._load_section(identifier, key)

    def _load_section(self, identifier, section):
        self._request_generation[section] += 1
        request = self._request_generation[section]
        self._loaded[section] = False
        self._loading.add(section)
        self._update_enabled()
        if section == "calibrations":
            self.calibration_version.hide()

        def current():
            return identifier == self._active_device and request == self._request_generation[section]

        def loaded(rows):
            if not current():
                return
            self._loading.discard(section)
            self._loaded[section] = True
            if section == "calibrations":
                self._calibrations = {(row["kind"], row["point"]): row for row in rows}
                version = self._devices[identifier].get("calibration_version", 0)
                self.save_calibration_button.setToolTip(tr('当前修正版本：{version}。仅对后续采集生效。', version=version))
                self._fill_calibration()
            else:
                self._thresholds = {row["kind"]: row for row in rows}
                self._fill_thresholds()
            self._update_enabled()

        def failed(message):
            if not current():
                return
            self._loading.discard(section)
            if section == "calibrations":
                self.calibration_version.setText(tr("微调参数未加载"))
                self.calibration_version.show()
            self._update_enabled()
            self._error(message)

        self.repo.call(section, identifier, callback=loaded, on_error=failed)

    def _calibration_values(self):
        return self.temp_offset.value(), self.humidity_offset.value()

    def _calibration_dirty(self):
        return self._loaded["calibrations"] and (
            self.batch_box.isChecked() or self._calibration_values() != self._calibration_baseline)

    def _threshold_text(self):
        return {field: editor.text().strip() for field, editor in self.threshold_inputs.items()}

    def _threshold_dirty(self):
        return self._loaded["thresholds"] and self._threshold_text() != self._threshold_baseline

    def _device_dirty(self):
        return self._calibration_dirty() or self._threshold_dirty()

    def _confirm_discard(self):
        return QMessageBox.question(
            self, tr("未保存的设备参数"), tr("放弃当前未保存的参数修改？"),
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Discard

    def _reload_device_parameters(self):
        if not self._saving and (not self._device_dirty() or self._confirm_discard()):
            self._load_parameters()

    def _kind_changed(self, *_):
        if not self._loaded["calibrations"]:
            return
        if self._calibration_dirty() and not self._confirm_discard():
            kind, point = self._calibration_point
            with QSignalBlocker(self.kind_combo):
                self.kind_combo.setCurrentIndex(self.kind_combo.findData(kind))
            self._sync_device_selectors(point)
            return
        self._sync_device_selectors()
        self._fill_calibration()
        self._update_enabled()

    def _fill_calibration(self):
        kind = self.kind_combo.currentData()
        self._calibration_point = kind, self.point_combo.currentData()
        row = self._calibrations.get(self._calibration_point, {})
        self.temp_offset.setValue(row.get("temp_offset", 0))
        self.humidity_offset.setValue(row.get("humidity_offset", 0) if kind == "ambient" else 0)
        with QSignalBlocker(self.batch_box):
            self.batch_box.setChecked(False)
        self._calibration_baseline = self._calibration_values()

    def _save_calibration(self):
        if not self._loaded["calibrations"] or self._saving:
            return
        kind, point = self._calibration_point
        points = self._points(kind)
        if point not in points:
            self._error(tr("当前测点不在设备范围内，请重新选择。"))
            return
        identifier = self._active_device
        if self.batch_box.isChecked():
            answer = QMessageBox.question(
                self, tr("确认批量微调"), tr('将修改当前设备的 {count} 个同类测点，是否继续？', count=len(points)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        else:
            points = [point]
        rows = [dict(kind=kind, point=p, temp_offset=self.temp_offset.value(),
                     humidity_offset=self.humidity_offset.value() if kind == "ambient" else 0) for p in points]
        self._saving = "calibrations"
        self._update_enabled()

        def saved(version):
            self._saving = None
            self._show_message(tr('微调参数已保存，共 {count} 个测点。仅对后续采集生效。', count=len(rows)))
            if identifier == self._active_device:
                self._devices[identifier]["calibration_version"] = version
                self._load_parameters("calibrations")
            self._update_enabled()

        self.repo.call("save_calibrations", identifier, rows, callback=saved, on_error=self._device_save_failed)

    def _threshold_changed(self, *_):
        if not self._loaded["thresholds"]:
            return
        if self._threshold_dirty() and not self._confirm_discard():
            with QSignalBlocker(self.threshold_kind):
                self.threshold_kind.setCurrentIndex(self.threshold_kind.findData(self._threshold_type))
            return
        self._fill_thresholds()
        self._update_enabled()

    def _fill_thresholds(self):
        self._threshold_type = self.threshold_kind.currentData()
        row = self._thresholds.get(self._threshold_type, {})
        for field, editor in self.threshold_inputs.items():
            value = row.get(field) if not field.startswith("humidity") or self._threshold_type == "ambient" else None
            editor.setText("" if value is None else str(value))
        self._threshold_baseline = self._threshold_text()

    def _save_threshold(self):
        if not self._loaded["thresholds"] or self._saving:
            return
        identifier, kind = self._active_device, self._threshold_type
        if not self._points(kind):
            self._error(tr("当前设备没有此类型的测点，请重新选择。"))
            return
        try:
            values = {}
            for field, editor in self.threshold_inputs.items():
                text = editor.text().strip() if editor.isEnabled() else ""
                value = float(text) if text else None
                if value is not None and not math.isfinite(value):
                    raise ValueError
                values[field] = value
        except ValueError:
            self._error(tr("阈值必须填写数字，留空表示不启用。"))
            return
        self._saving = "thresholds"
        self._update_enabled()

        def saved(_):
            self._saving = None
            self._show_message(tr("报警阈值已保存，将根据后续有效采样判断。"))
            if identifier == self._active_device:
                self._load_parameters("thresholds")
            self._update_enabled()

        self.repo.call("save_threshold", identifier, kind, callback=saved, on_error=self._device_save_failed, **values)

    def _device_save_failed(self, message):
        self._saving = None
        self._update_enabled()
        self._error(message)

    def _alarms_changed(self, rows):
        self._alarms = rows
        self.alarm_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            source = tr("模拟数据") if row["source"] == "simulator" else tr("串口设备")
            point_type = tr("粮温") if row["kind"] == "grain" else tr("仓温仓湿")
            point = f"{point_type} {row['point']}"
            measure = tr("温度") if row["metric"] == "temp" else tr("湿度")
            metric = (tr("{metric}低于下限", metric=measure) if row["direction"] == "low" else
                      tr("{metric}高于上限", metric=measure))
            values = [row["timestamp"], row.get("device_name", str(row["device_id"])), source, point, metric,
                      f"{row['value']:.2f}", f"{row['threshold']:.2f}", tr("当前报警") if row["active"] else tr("已恢复"),
                      row.get("acknowledged_at") or tr("未确认")]
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
            self._show_message(tr("请选择当前且未确认的报警；已恢复或已确认的记录保持不变。"))

    def _acknowledge(self, identifiers):
        def saved(count):
            self._show_message(tr('已确认并消音{count}条当前报警，记录继续保留。恢复后再次越限会形成新的报警。', count=count))
            self.controller.refresh_alarms()
        self.repo.call("acknowledge_alarms", identifiers, callback=saved, on_error=self._error)

    def _error(self, message):
        message = tr(str(message))
        self._show_message(message)
        QMessageBox.warning(self, tr("操作未完成"), message)

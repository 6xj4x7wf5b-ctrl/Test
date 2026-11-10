from __future__ import annotations

import math
import time

import pyqtgraph as pg
from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QMenu, QPushButton, QScrollArea, QSpinBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .devices_ui import button, fill_table, table
from .filter_panel import FilterPanel
from .i18n import tr
from .silo_model import build_silo_snapshot
from .silo_ui import SiloView

STATUS_NAMES = {"ok": "正常", "unbound": "未绑定", "not_ready": "尚未采样", "offline": "离线",
                "crc_error": "校验错误", "bus_error": "总线故障", "stale": "数据过期",
                "power_error": "供电错误", "timeout": "转换超时", "invalid": "数据无效", "data_error": "数据格式无效",
                "unknown": "无有效数据", "unknown_255": "无有效数据"}


def number(value, unit=""):
    return "—" if value is None else f"{value:.1f}{unit}"


class LivePage(QWidget):
    action_requested = Signal(str)

    def __init__(self, repository, controller, parent=None):
        super().__init__(parent)
        self.repo, self.controller = repository, controller
        self.layouts = {}
        self._alarms = []
        self._display_rows = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        filters = FilterPanel()
        self.warehouse = QComboBox()
        self.warehouse.setPlaceholderText(tr("暂无仓房"))
        self.device = QComboBox()
        self.device.setPlaceholderText(tr("暂无分机"))
        for label, selector in (("仓房", self.warehouse), ("分机", self.device)):
            selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            selector.setMinimumContentsLength(8)
            filters.add_field(tr(label), selector)
        self.source = QLabel(tr("模拟数据"))
        self.source.setObjectName("muted")
        self.source.setStyleSheet("color:#856128; font-weight:600;")
        self.source.hide()
        filters.add_action(self.source)
        self.communication = QLabel()
        self.communication.setObjectName("muted")
        self.communication.setAccessibleName(tr("当前分机通信"))
        self.communication.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.communication.setMinimumWidth(self.communication.fontMetrics().horizontalAdvance(tr("串口已打开 · 待查询")))
        self.communication.setMinimumHeight(self.device.minimumSizeHint().height())
        filters.add_action(self.communication)
        layout.addWidget(filters)

        summary = QFrame()
        summary.setObjectName("section")
        metrics = QHBoxLayout(summary)
        metrics.setContentsMargins(10, 6, 10, 6)
        metrics.setSpacing(14)
        self.metrics = []
        for title in ("有效测点", "待处理报警", "仓温 / 仓湿", "粮温范围 / 平均"):
            title = tr(title)
            content = QVBoxLayout()
            content.setSpacing(2)
            label = QLabel(title)
            label.setObjectName("muted")
            value = QLabel("—")
            value.setObjectName("sectionTitle")
            value.setAccessibleName(title)
            content.addWidget(label)
            content.addWidget(value)
            self.metrics.append(value)
            metrics.addLayout(content, 1)
        layout.addWidget(summary)
        self.plot = pg.PlotWidget(background="w")
        self.plot.setMinimumHeight(130)
        self.plot.setLabel("left", tr("温度"), units="℃", **{"font-size": "11px"})
        self.plot.setLabel("bottom", tr("粮温测点编号"), **{"font-size": "11px"})
        tick_font = QFont(self.plot.font())
        tick_font.setPixelSize(11)
        for name in ("left", "bottom"):
            axis = self.plot.getAxis(name)
            axis.setPen("#b4c0cd")
            axis.setTextPen("#53667a")
            axis.setStyle(tickFont=tick_font)
        self.plot.showGrid(x=True, y=True, alpha=0.12)
        self.curve = self.plot.plot(pen=pg.mkPen("#2f6fdf", width=2), symbol="o", symbolSize=4,
                                    symbolBrush="#2f6fdf", connect="finite")
        stage = QFrame()
        stage.setObjectName("stage")
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(8, 7, 8, 8)
        stage_layout.setSpacing(7)
        controls = QHBoxLayout()
        controls.setSpacing(8)
        stage_title = QLabel(tr("测温视图"))
        stage_title.setObjectName("sectionTitle")
        controls.addWidget(stage_title)
        self.reading_status = QLabel()
        self.reading_status.setObjectName("muted")
        self.reading_status.setWordWrap(True)
        controls.addWidget(self.reading_status, 1)
        self.collect_button = button(tr("采集一次"), self.collect_current, True)
        self.collect_button.setToolTip(tr("立即采集所选分机"))
        controls.addWidget(self.collect_button)
        self.history_button = button(tr("历史曲线"), lambda: self.action_requested.emit("history"))
        self.history_button.setObjectName("quietButton")
        controls.addWidget(self.history_button)
        self.more_button = QPushButton(tr("更多"))
        self.more_button.setObjectName("quietButton")
        self.more_button.setAccessibleName(tr("更多分机操作"))
        menu = QMenu(self.more_button)
        menu.addAction(tr("查询分机在线状态"), self.query_current)
        menu.addSeparator()
        for title, action in (("探头登记", "register"), ("调整布点", "layout"),
                              ("温湿度微调", "calibration"), ("导出报表", "export")):
            menu.addAction(tr(title), lambda checked=False, key=action: self.action_requested.emit(key))
        self.more_button.setMenu(menu)
        controls.addWidget(self.more_button)
        stage_layout.addLayout(controls)
        self.tabs = QTabWidget()
        self.silo_view = SiloView()
        self.silo_view.edit_requested.connect(self.edit_layout)
        self.silo_view.point_selected.connect(self._select_silo_point)
        self.silo_view.edit_button.hide()
        self.silo_view.message_label.hide()
        silo_controls = self.silo_view.layout().itemAt(0).layout()
        silo_controls.setStretchFactor(self.silo_view.layer_combo, 0)
        silo_controls.insertStretch(silo_controls.indexOf(self.silo_view.expand_button), 1)
        self.silo_view.expand_button.setObjectName("quietButton")
        self.tabs.addTab(self.silo_view, tr("粮仓立体图"))
        headers = [tr(title) for title in ("测点", "线 / 层 / 位置", "原温 ℃", "修正温 ℃", "湿度 %RH", "状态", "最近接收", "来源")]
        self.table = table(headers)
        for column, title in enumerate(headers):
            self.table.horizontalHeaderItem(column).setToolTip(title)
        self.table.horizontalHeaderItem(6).setToolTip(tr("上位机接收数据时的电脑时间"))
        self.tabs.addTab(self.table, tr("测点列表"))
        self.matrix_scroll = QScrollArea()
        self.matrix_scroll.setWidgetResizable(True)
        self.matrix_widget = QWidget()
        self.matrix = QGridLayout(self.matrix_widget)
        self.matrix.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.matrix.setContentsMargins(8, 8, 8, 8)
        self.matrix.setSpacing(6)
        self._matrix_cells = []
        self._matrix_columns = 0
        self._matrix_reflow_timer = QTimer(self)
        self._matrix_reflow_timer.setSingleShot(True)
        self._matrix_reflow_timer.timeout.connect(self._reflow_matrix)
        self.matrix_scroll.setWidget(self.matrix_widget)
        self.matrix_scroll.viewport().installEventFilter(self)
        self.tabs.addTab(self.matrix_scroll, tr("测点平铺图"))
        self.tabs.addTab(self.plot, tr("粮温分布"))
        self.tabs.setCurrentIndex(0)
        self.tabs.setMinimumHeight(250)
        stage_layout.addWidget(self.tabs, 1)
        layout.addWidget(stage, 1)
        self.warehouse.currentIndexChanged.connect(self._filter_devices)
        self.device.currentIndexChanged.connect(self.refresh)
        self.tabs.currentChanged.connect(self.refresh)
        controller.metadata_changed.connect(self.metadata_changed)
        controller.samples_changed.connect(lambda *_: self.refresh())
        controller.connections_changed.connect(self.refresh)
        controller.alarms_changed.connect(self._alarms_changed)
        self.repo.ready.connect(lambda: self.repo.call("get_setting", "point_layouts", {}, callback=self._layouts_loaded))
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(2000)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start()
        self.refresh()

    def collect_current(self):
        identifier = self.device.currentData()
        if identifier is not None:
            self.controller.poll(device_id=identifier)

    def query_current(self):
        device = self.controller.device(self.device.currentData())
        if device:
            self.controller.query_online(device)

    def eventFilter(self, watched, event):
        if watched is self.matrix_scroll.viewport() and event.type() == QEvent.Type.Resize:
            # Viewport width already accounts for the vertical scrollbar.
            # Defer until Qt finishes resizing the surrounding layouts.
            if not self._matrix_reflow_timer.isActive():
                self._matrix_reflow_timer.start(0)
        return super().eventFilter(watched, event)

    def _reflow_matrix(self):
        margins = self.matrix.contentsMargins()
        if not self._matrix_cells:
            self._matrix_columns = 0
            self.matrix_widget.setMinimumSize(margins.left() + margins.right(), margins.top() + margins.bottom())
            return
        spacing = max(0, self.matrix.horizontalSpacing())
        width = self.matrix_scroll.viewport().contentsRect().width() - margins.left() - margins.right()
        columns = max(1, min(16, (max(0, width) + spacing) // (64 + spacing)))
        if columns == self._matrix_columns:
            return
        while self.matrix.count():
            self.matrix.takeAt(0)
        for index, cell in enumerate(self._matrix_cells):
            self.matrix.addWidget(cell, index // columns, index % columns)
        self._matrix_columns = columns
        self.matrix.invalidate()
        # Newly added labels may not be visible until the next event turn, so
        # layout.minimumSize() can briefly omit them. Use their fixed geometry
        # to keep every row reachable while the scrollbar settles.
        occupied_columns = min(columns, len(self._matrix_cells))
        rows = (len(self._matrix_cells) + columns - 1) // columns
        vertical_spacing = max(0, self.matrix.verticalSpacing())
        self.matrix_widget.setMinimumSize(
            margins.left() + margins.right() + occupied_columns * 64 + (occupied_columns - 1) * spacing,
            margins.top() + margins.bottom() + rows * 44 + (rows - 1) * vertical_spacing,
        )

    def _alarms_changed(self, rows):
        self._alarms = rows
        self.refresh()

    def _layouts_loaded(self, value):
        self.layouts = value if isinstance(value, dict) else {}
        self.refresh()

    def metadata_changed(self, metadata):
        selected = self.warehouse.currentData()
        self.warehouse.blockSignals(True)
        self.warehouse.clear()
        warehouses = metadata["warehouses"]
        if warehouses:
            self.warehouse.addItem(tr("全部仓房"), None)
        for row in warehouses:
            self.warehouse.addItem(row["name"], row["id"])
        self.warehouse.setCurrentIndex(max(0, self.warehouse.findData(selected)))
        self.warehouse.blockSignals(False)
        self._filter_devices()

    def _filter_devices(self):
        selected = self.device.currentData()
        self.device.blockSignals(True)
        self.device.clear()
        warehouse_id = self.warehouse.currentData()
        for row in self.controller.metadata["devices"]:
            if warehouse_id in (None, row["warehouse_id"]):
                name = f"{row['name']} · {row['warehouse_name']}" if warehouse_id is None else row["name"]
                self.device.addItem(name, row["id"])
        self.device.setCurrentIndex(max(0, self.device.findData(selected)))
        self.device.blockSignals(False)
        self.refresh()

    def _layout_label(self, identifier, row):
        saved = self.layouts.get(str(identifier), {}).get(f"{row['kind']}:{row['point']}", {})
        return " / ".join(str(saved.get(key, "")) for key in ("line", "layer", "position") if saved.get(key)) or "—"

    def _select_silo_point(self, point):
        for index, row in enumerate(self._display_rows):
            if row["kind"] == "grain" and row["point"] == point:
                self.table.selectRow(index)
                break

    def _clear_matrix(self):
        while self.matrix.count():
            item = self.matrix.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._matrix_cells = []
        self._matrix_columns = 0

    def refresh(self, *_):
        identifier = self.device.currentData()
        device = self.controller.device(identifier)
        for control in (self.collect_button, self.history_button, self.more_button):
            control.setEnabled(device is not None)
        self.source.setVisible(bool(device and device["source"] == "simulator" and not self.controller.demo))
        if not device:
            self.communication.clear()
            self.communication.setToolTip("")
            self.device.setToolTip("")
            for metric in self.metrics:
                metric.setText("—")
            self.metrics[1].setStyleSheet("")
            self.reading_status.clear()
            self.table.setRowCount(0)
            self.curve.setData([], [])
            self._display_rows = []
            self.silo_view.set_snapshot(build_silo_snapshot(None, [], {}, connected=False, stale=False))
            self.silo_view.message_label.hide()
            self.silo_view.layer_combo.setToolTip("")
            self._clear_matrix()
            self._reflow_matrix()
            return
        self.device.setToolTip(self.device.currentText())
        self.communication.setText(tr(self.controller.communication_state(device)))
        self.communication.setToolTip(tr("{connection} · {port}\n{protocol} · 地址 {address}",
                                         connection=device["connection_name"], port=device["port_name"],
                                         protocol=device["protocol"], address=device["address"]))
        rows = self.controller.latest.get(identifier, [])
        age = time.monotonic() - self.controller.last_received.get(identifier, -1e10)
        stale = age > max(30, 3 * self.controller.timer.interval() / 1000)
        port_connected = self.controller.manager.is_connected(str(device["port_id"]))
        valid = [r for r in rows if r["status"] == "ok" and not stale and port_connected]
        grain = [r["temp"] for r in valid if r["kind"] == "grain" and r["temp"] is not None]
        ambient = next((r for r in valid if r["kind"] == "ambient"), None)
        self.metrics[0].setText(f"{len(valid)} / {len(self.controller.point_keys(device))}")
        alarm_count = sum(r.get('active', 0) and not r.get('acknowledged_at') for r in self._alarms if r.get('device_id') == identifier)
        self.metrics[1].setText(tr("{count} 条", count=alarm_count))
        alarm_style = "color:#a03e3e;" if alarm_count else ""
        if self.metrics[1].styleSheet() != alarm_style:
            self.metrics[1].setStyleSheet(alarm_style)
        self.metrics[2].setText(f"{number(ambient['temp'], '℃')} / {number(ambient['humidity'], '%RH')}" if ambient else "— / —")
        self.metrics[3].setText(f"{min(grain):.1f}～{max(grain):.1f}℃ / {sum(grain) / len(grain):.1f}℃" if grain else "— / —")
        if not rows:
            self.reading_status.setText(tr("尚无读数"))
        elif not port_connected:
            self.reading_status.setText(tr("连接断开 · 仅保留上次读数"))
        elif stale:
            self.reading_status.setText(tr("数据已过期 · 仅保留上次读数"))
        else:
            self.reading_status.clear()
        display = []
        for row in rows:
            state = "连接断开" if not port_connected else "已过期" if stale else STATUS_NAMES.get(row["status"], row["status"])
            point_label = tr("粮温 {point}" if row["kind"] == "grain" else "仓温湿 {point}", point=row["point"])
            display.append([point_label,
                            self._layout_label(identifier, row), number(row["raw_temp"]), number(row["temp"]),
                            number(row["humidity"]), tr(state), row["timestamp"][11:],
                            tr("模拟" if row["source"] == "simulator" else "实机")])
        fill_table(self.table, display)
        if stale or not port_connected:
            for r in range(self.table.rowCount()):
                for c in range(self.table.columnCount()):
                    self.table.item(r, c).setForeground(QColor("#7d8995"))
                    self.table.item(r, c).setToolTip(tr("这是上次读数，不能代表当前温湿度。"))
        self._display_rows = rows
        snapshot = build_silo_snapshot(
            device, rows, self.layouts.get(str(identifier), {}),
            connected=port_connected, stale=stale, alarms=self._alarms)
        self.silo_view.set_snapshot(snapshot)
        self.silo_view.layer_combo.setToolTip(snapshot.message)
        conflicts = sum(not point.registered and bool(point.cable and point.layer)
                        for layer in snapshot.layers for point in layer.points)
        message = tr("该分机未配置粮温测点") if not snapshot.grain_count else (
            tr("{count} 个测点的线 / 层重复", count=conflicts) if conflicts else "")
        self.silo_view.message_label.setText(message)
        self.silo_view.message_label.setVisible(bool(message))
        points = sorted((r["point"], r["temp"] if r in valid else math.nan) for r in rows if r["kind"] == "grain")
        if not any(math.isfinite(value) for _, value in points):
            points = []
        self.curve.setData([p[0] for p in points], [p[1] for p in points])
        if self.tabs.currentWidget() is self.matrix_scroll:
            self._clear_matrix()
            for row in rows:
                is_valid = row in valid
                point_label = tr("粮 {point:03}" if row["kind"] == "grain" else "仓 {point:03}", point=row["point"])
                cell = QLabel(f"{point_label}\n{number(row['temp'], '℃') if is_valid else '—'}")
                cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cell.setFixedSize(64, 44)
                cell.setToolTip(self._layout_label(identifier, row))
                color = "#f5f7f9" if is_valid else "#fff2f0"
                cell.setStyleSheet(f"background:{color}; color:#293849; border:1px solid #cbd3dd; border-radius:1px; padding:3px;")
                self._matrix_cells.append(cell)
            self._reflow_matrix()

    def edit_layout(self):
        identifier = self.device.currentData()
        device = self.controller.device(identifier)
        if not device:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("{device} · 测点布置", device=device["name"]))
        dialog.resize(650, 550)
        layout = QVBoxLayout(dialog)
        widget = QTableWidget(0, 4)
        widget.setHorizontalHeaderLabels([tr(title) for title in ("测点", "测温线", "层", "位置名称")])
        for column in (1, 2):
            widget.horizontalHeaderItem(column).setToolTip(tr("同一测温线的同一层只能放置一个粮温测点"))
        widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        keys = self.controller.point_keys(device)
        widget.setRowCount(len(keys))
        saved = self.layouts.get(str(identifier), {})
        for i, (kind, point) in enumerate(keys):
            record = saved.get(f"{kind}:{point}", {})
            item = QTableWidgetItem(tr("粮温 {point}" if kind == "grain" else "仓温湿 {point}", point=point))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            widget.setItem(i, 0, item)
            for col, key in enumerate(("line", "layer", "position"), 1):
                widget.setItem(i, col, QTableWidgetItem(str(record.get(key, ""))))
        generate_row = QHBoxLayout()
        generate_row.addWidget(QLabel(tr("每条测温线层数")))
        layer_count = QSpinBox()
        layer_count.setRange(1, max(1, device["grain_count"]))
        layer_count.setMaximumWidth(90)
        generate_row.addWidget(layer_count)
        generate = QPushButton(tr("按点号填充线 / 层"))
        generate.setEnabled(device["grain_count"] > 0)
        generate.setToolTip(tr("依次填写第1线各层、第2线各层。保存后更新本地测点视图。"))

        def generate_layout():
            for i, (kind, point) in enumerate(keys):
                if kind == "grain":
                    widget.item(i, 1).setText(str((point - 1) // layer_count.value() + 1))
                    widget.item(i, 2).setText(str((point - 1) % layer_count.value() + 1))

        generate.clicked.connect(generate_layout)
        generate_row.addWidget(generate)
        generate_row.addStretch()
        layout.addLayout(generate_row)
        layout.addWidget(widget)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        actions.accepted.connect(dialog.accept)
        actions.rejected.connect(dialog.reject)
        layout.addWidget(actions)
        if dialog.exec():
            result = dict(self.layouts)
            result[str(identifier)] = {**saved, **{f"{kind}:{point}": {key: widget.item(i, col).text().strip()
                                      for col, key in enumerate(("line", "layer", "position"), 1)}
                                      for i, (kind, point) in enumerate(keys)}}
            self.repo.call("set_setting", "point_layouts", result, callback=lambda _: self._layouts_loaded(result))

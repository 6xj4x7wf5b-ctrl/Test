from __future__ import annotations

import math
import time

import pyqtgraph as pg
from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QScrollArea, QSpinBox, QPushButton, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView, QBoxLayout, QSizePolicy,
)

from .devices_ui import button, fill_table, table
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
        layout.setSpacing(12)
        hero = QFrame()
        hero.setObjectName("hero")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(12, 10, 12, 10)
        hero_layout.setSpacing(7)
        hero.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        kicker = QLabel("粮情监测")
        kicker.setObjectName("eyebrow")
        hero_layout.addWidget(kicker)
        self.hero_title = QLabel("选择仓房")
        self.hero_title.setObjectName("cardTitle")
        hero_layout.addWidget(self.hero_title)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("仓房"))
        self.warehouse = QComboBox()
        self.device = QComboBox()
        self.warehouse.setMinimumWidth(140)
        self.warehouse.setMaximumWidth(210)
        self.device.setMinimumWidth(190)
        self.device.setMaximumWidth(280)
        filters.addWidget(self.warehouse)
        filters.addWidget(QLabel("分机"))
        filters.addWidget(self.device)
        filters.addStretch()
        hero_layout.addLayout(filters)
        metrics = QGridLayout()
        metrics.setSpacing(6)
        self.metrics = []
        metric_titles = ("串口连接", "有效测点", "报警信息", "仓内环境 · ℃ / %RH", "粮温区间", "平均粮温")
        tones = ("#607b96", "#7b8796", "#92745b", "#5e7fa1", "#947378", "#93816a")
        for i, title in enumerate(metric_titles):
            card = QFrame()
            card.setObjectName("detailMetric")
            card.setFixedHeight(50)
            card.setStyleSheet(f"QFrame#detailMetric {{ background:#f4f6f8; border:1px solid #cfd6df; border-left:3px solid {tones[i]}; border-radius:2px; }}")
            content = QVBoxLayout(card)
            content.setContentsMargins(10, 6, 10, 6)
            content.setSpacing(3)
            label = QLabel(title)
            label.setStyleSheet("color:#68768a; font-size:11px; font-weight:600; background:transparent;")
            value = QLabel("—")
            value.setStyleSheet("color:#213043; font-size:14px; font-weight:700; background:transparent;")
            content.addWidget(label)
            content.addWidget(value)
            self.metrics.append(value)
            metrics.addWidget(card, i // 3, i % 3)
        hero_layout.addLayout(metrics)
        self.note = QLabel("尚未配置设备。请在“设备与连接”中添加仓房、串口和分机。")
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        hero_layout.addWidget(self.note)
        layout.addWidget(hero)
        self.plot = pg.PlotWidget(background="w")
        self.plot.setMinimumHeight(130)
        self.plot.setLabel("left", "温度", units="℃", **{"font-size": "11px"})
        self.plot.setLabel("bottom", "粮温测点编号", **{"font-size": "11px"})
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
        self.workspace = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.workspace.setSpacing(12)
        stage = QFrame()
        stage.setObjectName("stage")
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(10, 10, 10, 10)
        stage_layout.setSpacing(6)
        stage_kicker = QLabel("粮仓测点")
        stage_kicker.setObjectName("eyebrow")
        stage_layout.addWidget(stage_kicker)
        stage_title = QLabel("温度点视图")
        stage_title.setObjectName("cardTitle")
        stage_layout.addWidget(stage_title)
        self.tabs = QTabWidget()
        self.silo_view = SiloView()
        self.silo_view.edit_requested.connect(self.edit_layout)
        self.silo_view.point_selected.connect(self._select_silo_point)
        self.tabs.addTab(self.silo_view, "粮仓立体图")
        self.table = table(["测点", "线 / 层 / 位置", "原温 ℃", "修正温 ℃", "湿度 %RH", "状态", "最近接收", "来源"])
        self.tabs.addTab(self.table, "测点列表")
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
        self.tabs.addTab(self.matrix_scroll, "测点平铺图")
        self.tabs.addTab(self.plot, "粮温分布")
        self.tabs.setCurrentIndex(0)
        self.tabs.setMinimumHeight(250)
        stage_layout.addWidget(self.tabs, 1)
        self.workspace.addWidget(stage, 1)
        self.console = QFrame()
        self.console.setObjectName("console")
        console = QVBoxLayout(self.console)
        console.setContentsMargins(12, 10, 12, 10)
        console.setSpacing(8)
        caption = QLabel("实时控制台")
        caption.setObjectName("eyebrow")
        console.addWidget(caption)
        caption = QLabel("功能操作")
        caption.setObjectName("cardTitle")
        console.addWidget(caption)
        console.addWidget(button("采集粮情", controller.poll, True))
        console.addWidget(button("刷新状态", controller.poll))
        entries = QGridLayout()
        for i, (text, action) in enumerate((("探头登记", "register"), ("调整布点", "layout"),
                                             ("温湿度微调", "calibration"), ("历史曲线", "history"),
                                             ("导出报表", "export"))):
            entries.addWidget(button(text, lambda _, a=action: self.action_requested.emit(a)), i // 2, i % 2)
        console.addLayout(entries)
        console.addStretch()
        self.console_summary = QLabel("尚未选择分机")
        self.console_summary.setObjectName("muted")
        self.console_summary.setWordWrap(True)
        console.addWidget(self.console_summary)
        self.workspace.addWidget(self.console)
        layout.addLayout(self.workspace, 1)
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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        side_by_side = self.width() >= 800
        self.workspace.setDirection(QBoxLayout.Direction.LeftToRight if side_by_side else QBoxLayout.Direction.TopToBottom)
        self.console.setMinimumWidth(230 if side_by_side else 0)
        self.console.setMaximumWidth(250 if side_by_side else 16777215)

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
        self.warehouse.addItem("全部仓房", None)
        for row in metadata["warehouses"]:
            self.warehouse.addItem(row["name"], row["id"])
        self.warehouse.setCurrentIndex(max(0, self.warehouse.findData(selected)))
        self.warehouse.blockSignals(False)
        self._filter_devices()

    def _filter_devices(self):
        selected = self.device.currentData()
        self.device.blockSignals(True)
        self.device.clear()
        for row in self.controller.metadata["devices"]:
            if self.warehouse.currentData() in (None, row["warehouse_id"]):
                self.device.addItem(f"{row['name']} · {row['warehouse_name']}", row["id"])
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
        connected = sum(self.controller.manager.is_connected(str(p["id"])) for p in self.controller.metadata["ports"])
        self.metrics[0].setText(str(connected))
        identifier = self.device.currentData()
        device = self.controller.device(identifier)
        if not device:
            for metric in self.metrics[1:]:
                metric.setText("—")
            self.hero_title.setText("尚未配置仓房与分机")
            self.table.setRowCount(0)
            self.curve.setData([], [])
            self._display_rows = []
            self.silo_view.set_snapshot(build_silo_snapshot(None, [], {}, connected=False, stale=False))
            self._clear_matrix()
            self._reflow_matrix()
            self.note.setText("尚未配置设备。请在“设备与连接”中添加仓房、串口和分机。")
            return
        self.hero_title.setText(f"{device['warehouse_name']} · {device['name']}")
        rows = self.controller.latest.get(identifier, [])
        age = time.monotonic() - self.controller.last_received.get(identifier, -1e10)
        stale = age > max(30, 3 * self.controller.timer.interval() / 1000)
        port_connected = self.controller.manager.is_connected(str(device["port_id"]))
        valid = [r for r in rows if r["status"] == "ok" and not stale and port_connected]
        grain = [r["temp"] for r in valid if r["kind"] == "grain" and r["temp"] is not None]
        ambient = next((r for r in valid if r["kind"] == "ambient"), None)
        self.metrics[1].setText(f"{len(valid)} / {device['grain_count'] + device['ambient_count']}")
        alarm_count = sum(r.get('active', 0) and not r.get('acknowledged_at') for r in self._alarms if r.get('device_id') == identifier)
        self.metrics[2].setText(f"{alarm_count} 条待处理" if alarm_count else "当前无温度预警")
        self.metrics[3].setText(f"{number(ambient['temp'])} / {number(ambient['humidity'])}" if ambient else "— / —")
        self.metrics[4].setText(f"{min(grain):.1f} ～ {max(grain):.1f}℃" if grain else "—")
        self.metrics[5].setText(number(sum(grain) / len(grain), "℃") if grain else "—")
        source = "模拟设备" if device["source"] == "simulator" else "现场设备"
        state = "已连接" if port_connected else "未连接"
        self.note.setText(f"{source} · {device['protocol']} · 地址 {device['address']} · {state}　"
                          "最近接收时间为电脑时间；立体图为线 / 层布点示意，仓温仓湿独立显示。")
        self.console_summary.setText(f"{device['connection_name']}\n分机地址 {device['address']}\n"
                                     f"粮温 {device['grain_count']} 点 · 仓温湿 {device['ambient_count']} 点\n{source} · {state}")
        display = []
        for row in rows:
            state = "连接断开" if not port_connected else "数据过期" if stale else STATUS_NAMES.get(row["status"], row["status"])
            display.append([f"{'粮温' if row['kind'] == 'grain' else '仓温湿'} {row['point']}",
                            self._layout_label(identifier, row), number(row["raw_temp"]), number(row["temp"]),
                            number(row["humidity"]), state, row["timestamp"][11:],
                            "模拟" if row["source"] == "simulator" else "实机"])
        fill_table(self.table, display)
        self._display_rows = rows
        self.silo_view.set_snapshot(build_silo_snapshot(
            device, rows, self.layouts.get(str(identifier), {}),
            connected=port_connected, stale=stale, alarms=self._alarms))
        points = sorted((r["point"], r["temp"] if r in valid else math.nan) for r in rows if r["kind"] == "grain")
        if not any(math.isfinite(value) for _, value in points):
            points = []
        self.curve.setData([p[0] for p in points], [p[1] for p in points])
        if self.tabs.currentWidget() is self.matrix_scroll:
            self._clear_matrix()
            for row in rows:
                is_valid = row in valid
                cell = QLabel(f"{'粮' if row['kind'] == 'grain' else '仓'} {row['point']:03}\n{number(row['temp'], '℃') if is_valid else '—'}")
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
        dialog.setWindowTitle(f"{device['name']} · 测点布置")
        dialog.resize(650, 550)
        layout = QVBoxLayout(dialog)
        hint = QLabel("线、层相同的粮温点会被视为布点冲突；立体图按这些标签分层展示。仓温仓湿独立显示。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        widget = QTableWidget(0, 4)
        widget.setHorizontalHeaderLabels(["测点", "测温线", "层", "位置名称"])
        widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        keys = [(kind, p) for kind, count in (("grain", device["grain_count"]), ("ambient", device["ambient_count"])) for p in range(1, count + 1)]
        widget.setRowCount(len(keys))
        saved = self.layouts.get(str(identifier), {})
        for i, (kind, point) in enumerate(keys):
            record = saved.get(f"{kind}:{point}", {})
            item = QTableWidgetItem(f"{'粮温' if kind == 'grain' else '仓温湿'} {point}")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            widget.setItem(i, 0, item)
            for col, key in enumerate(("line", "layer", "position"), 1):
                widget.setItem(i, col, QTableWidgetItem(str(record.get(key, ""))))
        generate_row = QHBoxLayout()
        generate_row.addWidget(QLabel("每条测温线层数"))
        layer_count = QSpinBox()
        layer_count.setRange(1, max(1, device["grain_count"]))
        layer_count.setMaximumWidth(90)
        generate_row.addWidget(layer_count)
        generate = QPushButton("按点号填充线 / 层")
        generate.setEnabled(device["grain_count"] > 0)
        generate.setToolTip("依次填写第1线各层、第2线各层；只修改当前表格，点击保存后生效。")

        def generate_layout():
            for i, (kind, point) in enumerate(keys):
                if kind == "grain":
                    widget.item(i, 1).setText(str((point - 1) // layer_count.value() + 1))
                    widget.item(i, 2).setText(str((point - 1) % layer_count.value() + 1))

        generate.clicked.connect(generate_layout)
        generate_row.addWidget(generate)
        generate_row.addStretch()
        layout.addLayout(generate_row)
        ordering = QLabel("填充顺序：第1线各层 → 第2线各层。请按现场实际层数填写；保存仅影响本地视图。")
        ordering.setWordWrap(True)
        ordering.setObjectName("muted")
        layout.addWidget(ordering)
        layout.addWidget(widget)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        actions.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        actions.accepted.connect(dialog.accept)
        actions.rejected.connect(dialog.reject)
        layout.addWidget(actions)
        if dialog.exec():
            result = dict(self.layouts)
            result[str(identifier)] = {f"{kind}:{point}": {key: widget.item(i, col).text().strip()
                                      for col, key in enumerate(("line", "layer", "position"), 1)}
                                      for i, (kind, point) in enumerate(keys)}
            self.repo.call("set_setting", "point_layouts", result, callback=lambda _: self._layouts_loaded(result))

"""Offline dashboard and grain overview, following the original Vue page order."""
from __future__ import annotations

import math
import time
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPushButton, QScrollArea, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QSizePolicy,
)
from .domain import configured_point_keys


def _label(text: str, name: str = "") -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(True)
    return widget


def _number(values: list[float], unit: str) -> str:
    return f"{sum(values) / len(values):.1f}{unit}" if values else "—"


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class _WarehouseCard(QFrame):
    activated = Signal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit()
        super().mouseReleaseEvent(event)


class OverviewPage(QWidget):
    detail_requested = Signal(int)
    history_requested = Signal()

    def __init__(self, repo, controller, parent=None):
        super().__init__(parent)
        self.repo, self.controller = repo, controller
        self.mode = "home"
        self._alarms: list[dict] = []
        self._alarms_loaded = False
        self._filters = ("", "all", "all")
        self._rows: list[dict] = []
        self._cards: dict[int, tuple[QFrame, dict[str, QLabel], QPushButton]] = {}
        self._page, self._page_size = 0, 12
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.body = QWidget()
        self.body.setObjectName("overviewBody")
        self.content = QVBoxLayout(self.body)
        self.content.setContentsMargins(14, 12, 14, 14)
        self.content.setSpacing(12)
        self._build_header()
        self._build_home()
        self._build_overview()
        self.content.addStretch()
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll)
        controller.metadata_changed.connect(self.refresh)
        controller.samples_changed.connect(self.refresh)
        controller.connections_changed.connect(self.refresh)
        controller.alarms_changed.connect(self._alarms_changed)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self._visible_refresh)
        self.timer.start()
        self.set_mode("home")

    def _build_header(self):
        row = QHBoxLayout()
        copy = QVBoxLayout()
        copy.setSpacing(2)
        self.kicker = _label("运行概况", "eyebrow")
        self.title = _label("首页", "pageTitle")
        copy.addWidget(self.kicker)
        copy.addWidget(self.title)
        row.addLayout(copy)
        row.addStretch()
        self.mode_label = _label("模拟数据 · 本地数据库" if self.controller.demo else "本地串口 · 离线运行", "muted")
        self.mode_label.setWordWrap(False)
        self.mode_label.setMinimumWidth(200)
        self.mode_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.mode_label)
        self.content.addLayout(row)

    def _build_home(self):
        self.home_frame = QFrame()
        self.home_frame.setObjectName("section")
        self.home_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        home = QVBoxLayout(self.home_frame)
        home.setContentsMargins(12, 10, 12, 12)
        home.setSpacing(10)
        heading = QHBoxLayout()
        heading.addWidget(_label("粮情监测概况", "cardTitle"))
        heading.addStretch()
        self.home_state = _label("尚无采集数据", "muted")
        heading.addWidget(self.home_state)
        home.addLayout(heading)
        grid = QGridLayout()
        grid.setSpacing(8)
        self.metric_values: list[QLabel] = []
        self.metric_notes: list[QLabel] = []
        specifications = [
            ("仓房数", "blue", "#2f6fdf", "#edf4fd"),
            ("在线分机", "green", "#49a56a", "#edf8f0"),
            ("有效粮温点", "violet", "#7883ea", "#eef1ff"),
            ("待处理报警", "orange", "#d28f3f", "#fff3e6"),
            ("仓温 / 仓湿", "blue", "#2f6fdf", "#edf4fd"),
            ("历史记录", "neutral", "#8da0b3", "#f3f7fb"),
        ]
        for index, (title, tone, accent, soft) in enumerate(specifications):
            card = QFrame()
            card.setObjectName("card")
            card.setProperty("tone", tone)
            card.setMinimumHeight(98)
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            card.setStyleSheet(f"QFrame#card {{ border:1px solid #b9c3ce; border-top:2px solid {accent}; border-radius:2px; background:#f7f9fb; }}")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(12, 10, 12, 10)
            layout.setSpacing(4)
            layout.addWidget(_label(title, "muted"))
            value = _label("—", "metric")
            value.setStyleSheet("font-size:22px;font-weight:700;background:transparent")
            note = _label("", "muted")
            if index == 5:
                value.hide()
                action = QPushButton("查看历史记录  →")
                action.clicked.connect(self.history_requested.emit)
                layout.addWidget(action, alignment=Qt.AlignmentFlag.AlignLeft)
            else:
                layout.addWidget(value)
            layout.addWidget(note)
            self.metric_values.append(value)
            self.metric_notes.append(note)
            grid.addWidget(card, index // 3, index % 3)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        home.addLayout(grid)
        self.home_note = _label("", "muted")
        home.addWidget(self.home_note)
        self.content.addWidget(self.home_frame)

    def _build_overview(self):
        self.filter_frame = QFrame()
        self.filter_frame.setObjectName("section")
        self.filter_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        filters = QVBoxLayout(self.filter_frame)
        filters.setContentsMargins(12, 10, 12, 12)
        filters.setSpacing(8)
        filters.addWidget(_label("筛选", "cardTitle"))
        row = QHBoxLayout()
        row.setSpacing(10)
        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText("请输入仓房名称")
        self.keyword.returnPressed.connect(self.apply_filters)
        self.alarm_filter, self.state_filter = QComboBox(), QComboBox()
        for label, key in (("全部", "all"), ("有预警", "any"), ("温度预警", "temp"), ("湿度预警", "humidity"), ("无预警", "none")):
            self.alarm_filter.addItem(label, key)
        for label, key in (("全部", "all"), ("在线", "online"), ("离线 / 过期", "offline"), ("尚未采集", "pending"), ("未配置分机", "unconfigured")):
            self.state_filter.addItem(label, key)
        for title, widget in (("仓房名称", self.keyword), ("报警类型", self.alarm_filter), ("分机状态", self.state_filter)):
            column = QVBoxLayout()
            column.setSpacing(3)
            column.addWidget(_label(title, "muted"))
            column.addWidget(widget)
            row.addLayout(column, 1)
        query, clear = QPushButton("查询"), QPushButton("清空")
        query.setObjectName("primary")
        query.clicked.connect(self.apply_filters)
        clear.clicked.connect(self.clear_filters)
        row.addWidget(query, alignment=Qt.AlignmentFlag.AlignBottom)
        row.addWidget(clear, alignment=Qt.AlignmentFlag.AlignBottom)
        filters.addLayout(row)
        self.content.addWidget(self.filter_frame)

        self.list_frame = QFrame()
        self.list_frame.setObjectName("section")
        self.list_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        result = QVBoxLayout(self.list_frame)
        result.setContentsMargins(12, 10, 12, 12)
        result.setSpacing(8)
        heading = QHBoxLayout()
        heading.addWidget(_label("粮情总览列表", "cardTitle"))
        heading.addStretch()
        self.count_label = _label("0 个仓房", "muted")
        heading.addWidget(self.count_label)
        self.refresh_button = QPushButton("刷新状态")
        self.refresh_button.clicked.connect(self.request_refresh)
        heading.addWidget(self.refresh_button)
        self.view_combo = QComboBox()
        self.view_combo.addItem("卡片展示", "card")
        self.view_combo.addItem("列表展示", "list")
        self.view_combo.currentIndexChanged.connect(self._render_rows)
        heading.addWidget(self.view_combo)
        result.addLayout(heading)
        self.views = QStackedWidget()
        self.card_area = QWidget()
        self.card_grid = QGridLayout(self.card_area)
        self.card_grid.setContentsMargins(0, 0, 0, 0)
        self.card_grid.setSpacing(8)
        self.card_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.views.addWidget(self.card_area)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["仓房名称", "仓房温度", "仓房湿度", "报警类型", "分机状态", "更新时间", "操作"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(lambda row, _: self._open_row(row))
        self.views.addWidget(self.table)
        self.views.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        result.addWidget(self.views)
        self.empty_label = _label("尚无仓房。请先在仓房信息管理中添加仓房，并关联测温分机。", "muted")
        result.addWidget(self.empty_label)
        pager = QHBoxLayout()
        pager.addStretch()
        self.prev_button, self.next_button = QPushButton("‹"), QPushButton("›")
        self.prev_button.clicked.connect(lambda: self._turn_page(-1))
        self.next_button.clicked.connect(lambda: self._turn_page(1))
        self.page_label = _label("1 / 1", "muted")
        pager.addWidget(self.prev_button)
        pager.addWidget(self.page_label)
        pager.addWidget(self.next_button)
        result.addLayout(pager)
        self.content.addWidget(self.list_frame)

    def set_mode(self, mode: str):
        if mode not in ("home", "overview"):
            raise ValueError("页面模式必须是 home 或 overview")
        self.mode = mode
        self.title.setText("首页" if mode == "home" else "粮情总览")
        self.kicker.setText("运行概况" if mode == "home" else "仓房粮情")
        self.home_frame.setVisible(mode == "home")
        self.filter_frame.setVisible(mode == "overview")
        self.list_frame.setVisible(mode == "overview")
        self.refresh()

    def _visible_refresh(self):
        if self.isVisible():
            self.refresh()

    def _alarms_changed(self, rows):
        self._alarms = rows if isinstance(rows, list) else []
        self._alarms_loaded = isinstance(rows, list)
        self.refresh()

    def _device_data(self, device: dict) -> tuple[str, list[dict]]:
        connected = self.controller.manager.is_connected(str(device["port_id"]))
        received = self.controller.last_received.get(device["id"])
        fresh = received is not None and time.monotonic() - received <= max(30, 3 * self.controller.timer.interval() / 1000)
        state = "online" if connected and fresh else "pending" if connected and received is None else "offline"
        rows = self.controller.latest.get(device["id"], [])
        valid = [row for row in rows if state == "online" and row.get("status") == "ok"
                 and row.get("source", device.get("source")) == device.get("source")]
        return state, valid

    def _summaries(self) -> tuple[list[dict], dict]:
        metadata = self.controller.metadata
        states, valid = {}, {}
        for device in metadata["devices"]:
            states[device["id"]], valid[device["id"]] = self._device_data(device)
        active_alarms = [row for row in self._alarms if row.get("active")]
        rows = []
        for warehouse in metadata["warehouses"]:
            devices = sorted((d for d in metadata["devices"] if d["warehouse_id"] == warehouse["id"]), key=lambda d: d["id"])
            ids = {d["id"] for d in devices}
            online = sum(states[d["id"]] == "online" for d in devices)
            state = "online" if online else "pending" if any(states[d["id"]] == "pending" for d in devices) else "offline" if devices else "unconfigured"
            samples = [sample for identifier in ids for sample in valid[identifier]]
            ambient = [sample for sample in samples if sample.get("kind") == "ambient"]
            temperature = [r["temp"] for r in ambient if _finite(r.get("temp"))]
            humidity = [r["humidity"] for r in ambient if _finite(r.get("humidity"))]
            alarms = [alarm for alarm in active_alarms if alarm.get("device_id") in ids]
            labels = {"temp": "温度预警", "humidity": "湿度预警"}
            alarm_text = "、".join(labels[key] for key in labels if any(a.get("metric") == key for a in alarms)) or ("有预警" if alarms else "无预警")
            if not self._alarms_loaded:
                alarm_text = "报警状态尚未读取"
            timestamps = [str(row.get("timestamp", "")) for identifier in ids for row in self.controller.latest.get(identifier, []) if row.get("timestamp")]
            state_label = {"online": f"在线 {online}/{len(devices)}", "pending": "尚未采集", "offline": "离线 / 数据过期", "unconfigured": "未配置分机"}[state]
            rows.append({"id": warehouse["id"], "name": warehouse["name"], "devices": devices,
                         "first_device": devices[0]["id"] if devices else None,
                         "temperature": _number(temperature, "℃"), "humidity": _number(humidity, "%RH"),
                         "state": state, "state_label": state_label, "alarms": alarms, "alarm_text": alarm_text,
                         "updated": max(timestamps).replace("T", " ") if timestamps else "尚未接收"})
        flattened = [row for values in valid.values() for row in values]
        pending = sum(1 for row in self._alarms if row.get("active") and not row.get("acknowledged_at"))
        stats = {"online": sum(state == "online" for state in states.values()),
                 "grain_valid": sum(row.get("kind") == "grain" and _finite(row.get("temp")) for row in flattened),
                 "ambient_temp": [row["temp"] for row in flattened if row.get("kind") == "ambient" and _finite(row.get("temp"))],
                 "ambient_humidity": [row["humidity"] for row in flattened if row.get("kind") == "ambient" and _finite(row.get("humidity"))],
                 "pending": pending}
        return rows, stats

    def refresh(self, *_):
        rows, stats = self._summaries()
        metadata = self.controller.metadata
        grain_capacity = sum(sum(k == "grain" for k, p in configured_point_keys(row)) for row in metadata["devices"])
        self.metric_values[0].setText(str(len(metadata["warehouses"])))
        self.metric_values[1].setText(str(stats["online"]))
        self.metric_values[2].setText(str(stats["grain_valid"]))
        self.metric_values[3].setText(("≥" if stats["pending"] >= 500 else "") + str(stats["pending"]) if self._alarms_loaded else "—")
        self.metric_values[4].setText(f"{_number(stats['ambient_temp'], '℃')} / {_number(stats['ambient_humidity'], '%RH')}")
        notes = ["本地仓房档案", f"共 {len(metadata['devices'])} 台，需串口连接且最近接收未过期",
                 f"已配置 {grain_capacity} 个粮温点，故障及过期值不计入", "正在触发且尚未确认的报警",
                 f"{len(stats['ambient_temp'])} 个有效环境测点的平均值", "查看本地历史曲线和采集记录"]
        for label, text in zip(self.metric_notes, notes):
            label.setText(text)
        self.home_state.setText("正在采集" if self.controller.running else "采集已暂停")
        self.home_note.setText("尚无仓房档案，请先添加仓房、串口和测温分机。" if not rows else
                               "仅使用本地接收的有效数据。串口已连接不代表全部分机在线；停止采集后旧读数会按时效失效。")
        keyword, alarm, state = self._filters
        self._rows = [row for row in rows if keyword.casefold() in row["name"].casefold()
                      and (state == "all" or row["state"] == state)
                      and (alarm == "all" or self._alarms_loaded and (alarm == "any" and row["alarms"]
                           or alarm == "none" and not row["alarms"]
                           or alarm in ("temp", "humidity") and any(a.get("metric") == alarm for a in row["alarms"])))]
        self.count_label.setText(f"{len(self._rows)} 个仓房")
        self._render_rows()

    def request_refresh(self):
        self.controller.refresh_alarms()
        self.controller.poll()
        self.refresh()

    def apply_filters(self):
        self._filters = (self.keyword.text().strip(), self.alarm_filter.currentData(), self.state_filter.currentData())
        self._page = 0
        self.refresh()

    def clear_filters(self):
        self.keyword.clear()
        self.alarm_filter.setCurrentIndex(0)
        self.state_filter.setCurrentIndex(0)
        self.apply_filters()

    def _turn_page(self, direction):
        self._page += direction
        self._render_rows()

    def _open_row(self, index):
        selected = self._rows[self._page * self._page_size:(self._page + 1) * self._page_size]
        if 0 <= index < len(selected) and selected[index]["first_device"] is not None:
            self.detail_requested.emit(selected[index]["first_device"])

    def _render_rows(self, *_):
        if not hasattr(self, "views"):
            return
        page_count = max(1, (len(self._rows) + self._page_size - 1) // self._page_size)
        self._page = max(0, min(self._page, page_count - 1))
        selected = self._rows[self._page * self._page_size:(self._page + 1) * self._page_size]
        self.page_label.setText(f"{self._page + 1} / {page_count}")
        self.prev_button.setEnabled(self._page > 0)
        self.next_button.setEnabled(self._page + 1 < page_count)
        self.empty_label.setVisible(not selected)
        if not selected:
            self.empty_label.setText("当前筛选条件下暂无仓房。" if self.controller.metadata["warehouses"] else "尚无仓房。请先在仓房信息管理中添加仓房，并关联测温分机。")
        card_mode = self.view_combo.currentData() == "card"
        self.views.setCurrentIndex(0 if card_mode else 1)
        self.views.setVisible(bool(selected))
        if not card_mode:
            self.table.setRowCount(len(selected))
            for index, row in enumerate(selected):
                for column, value in enumerate((row["name"], row["temperature"], row["humidity"], row["alarm_text"], row["state_label"], row["updated"])):
                    self.table.setItem(index, column, QTableWidgetItem(str(value)))
                action = QPushButton("查看粮情")
                action.setMaximumHeight(26)
                action.setMaximumWidth(94)
                action.setEnabled(row["first_device"] is not None)
                action.clicked.connect(lambda _, identifier=row["first_device"]: self.detail_requested.emit(identifier))
                actions = QWidget()
                action_layout = QHBoxLayout(actions)
                action_layout.setContentsMargins(2, 0, 2, 0)
                action_layout.addWidget(action, alignment=Qt.AlignmentFlag.AlignLeft)
                action_layout.addStretch()
                self.table.setCellWidget(index, 6, actions)
            self.views.setFixedHeight(min(420, max(90, self.table.horizontalHeader().height() + len(selected) * 30 + 6)))
            return
        selected_ids = {row["id"] for row in selected}
        for identifier in list(self._cards):
            if identifier not in selected_ids:
                card, _, _ = self._cards.pop(identifier)
                self.card_grid.removeWidget(card)
                card.deleteLater()
        columns = max(1, min(len(selected), 3 if self.body.width() >= 900 else 2))
        for index, row in enumerate(selected):
            if row["id"] not in self._cards:
                card = _WarehouseCard()
                card.setObjectName("card")
                card.setCursor(Qt.CursorShape.PointingHandCursor)
                card.setMinimumWidth(240)
                card.setMinimumHeight(148)
                card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                content = QVBoxLayout(card)
                content.setContentsMargins(12, 8, 12, 8)
                content.setSpacing(3)
                labels = {key: _label("", "cardTitle" if key == "name" else "muted") for key in ("name", "temperature", "humidity", "updated", "state_label", "alarm_text")}
                content.addWidget(labels["name"])
                environment = QHBoxLayout()
                environment.setSpacing(8)
                environment.addWidget(labels["temperature"])
                environment.addWidget(labels["humidity"])
                content.addLayout(environment)
                content.addWidget(labels["updated"])
                content.addWidget(labels["state_label"])
                action = QPushButton("查看粮情")
                action.setMaximumHeight(26)
                footer = QHBoxLayout()
                footer.addWidget(labels["alarm_text"], 1)
                footer.addWidget(action, alignment=Qt.AlignmentFlag.AlignRight)
                content.addLayout(footer)
                card.activated.connect(lambda identifier=row["id"]: self._open_warehouse(identifier))
                action.clicked.connect(lambda _, identifier=row["id"]: self._open_warehouse(identifier))
                self._cards[row["id"]] = card, labels, action
            card, labels, action = self._cards[row["id"]]
            labels["name"].setText(row["name"])
            labels["temperature"].setText("仓房温度：" + row["temperature"])
            labels["humidity"].setText("仓房湿度：" + row["humidity"])
            labels["updated"].setText("更新时间：" + row["updated"])
            labels["state_label"].setText("分机状态：" + row["state_label"])
            labels["alarm_text"].setText(row["alarm_text"])
            action.setEnabled(row["first_device"] is not None)
            self.card_grid.addWidget(card, index // columns, index % columns, Qt.AlignmentFlag.AlignTop)
            column_width = max(240, (self.body.width() - 54 - 8 * (columns - 1)) // columns)
            card_height = card.layout().heightForWidth(column_width)
            card.setFixedHeight(max(148, card_height + 2))
        for column in range(3):
            self.card_grid.setColumnStretch(column, 1 if column < columns else 0)
        self.card_grid.invalidate()
        width = max(240, self.body.width() - 54)
        height = self.card_grid.heightForWidth(width)
        if height < 0:
            height = self.card_area.sizeHint().height()
        self.views.setFixedHeight(max(148, height))

    def _open_warehouse(self, identifier):
        row = next((row for row in self._rows if row["id"] == identifier), None)
        if row and row["first_device"] is not None:
            self.detail_requested.emit(row["first_device"])

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "views") and self.mode == "overview":
            self._render_rows()

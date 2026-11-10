"""Offline dashboard and grain overview, following the original Vue page order."""
from __future__ import annotations

import math
import time
from typing import Any

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPushButton, QScrollArea, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QSizePolicy,
)
from .domain import configured_point_keys
from .filter_panel import FilterPanel
from .i18n import tr


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


class SummaryCard(QFrame):
    activated = Signal()

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self._pressed = False
        self.setAccessibleName(title)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("""
            QFrame#summaryCard:hover {
                border-top-color: #8da9ca;
                border-right-color: #8da9ca;
                border-bottom-color: #8da9ca;
            }
            QFrame#summaryCard:focus {
                border-top-color: #2f6fdf;
                border-right-color: #2f6fdf;
                border-bottom-color: #2f6fdf;
            }
        """)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self._pressed = True
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mouseReleaseEvent(event)
        pressed, self._pressed = self._pressed, False
        event.accept()
        if pressed and self.rect().contains(event.position().toPoint()):
            self.activated.emit()

    def keyPressEvent(self, event):
        if event.key() not in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            return super().keyPressEvent(event)
        event.accept()
        if not event.isAutoRepeat():
            self.activated.emit()


class OverviewPage(QWidget):
    detail_requested = Signal(int)
    history_requested = Signal()
    summary_requested = Signal(str)

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
        self.scroll.viewport().installEventFilter(self)
        self.body = QWidget()
        self.body.setObjectName("overviewBody")
        self.content = QVBoxLayout(self.body)
        self.content.setContentsMargins(0, 0, 0, 0)
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
        # The application header owns the page title and data-mode badge.
        self.kicker, self.title, self.mode_label = QLabel(self), QLabel(self), QLabel(self)
        for label in (self.kicker, self.title, self.mode_label):
            label.hide()

    def _build_home(self):
        self.home_frame = QFrame()
        self.home_frame.setObjectName("homeMetrics")
        self.home_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        home = QVBoxLayout(self.home_frame)
        home.setContentsMargins(0, 0, 0, 0)
        home.setSpacing(0)
        self.home_state = QLabel(self.home_frame)
        self.home_state.hide()
        grid = QGridLayout()
        grid.setSpacing(10)
        summary_keys = ("warehouses", "devices", "points", "alarms")
        self.metric_cards = []
        self.metric_values, self.metric_notes = [], []
        for index, title in enumerate((tr('仓房'), tr('在线分机'), tr('有效粮温点'), tr('待处理报警'), tr('仓温 / 仓湿'), tr('历史记录'))):
            card = SummaryCard(title, self.home_frame) if index < len(summary_keys) else QFrame(self.home_frame)
            card.setObjectName("summaryCard")
            card.setProperty("tone", ("blue", "green", "slate", "amber", "slate", "slate")[index])
            content = QVBoxLayout(card)
            content.setContentsMargins(14, 10, 12, 10)
            content.setSpacing(3)
            content.setAlignment(Qt.AlignmentFlag.AlignTop)
            content.addWidget(_label(title, "muted"))
            value = _label("—", "metric")
            value.setFixedHeight(30)
            content.addWidget(value)
            note = _label("", "sectionMeta")
            note.setFixedHeight(16)
            content.addWidget(note)
            self.metric_values.append(value)
            self.metric_notes.append(note)
            if index < len(summary_keys):
                card.activated.connect(lambda key=summary_keys[index]: self.summary_requested.emit(key))
                self.metric_cards.append(card)
                grid.addWidget(card, 0, index)
                grid.setColumnStretch(index, 1)
            else:
                card.hide()
        home.addLayout(grid)
        self.home_note = _label("", "muted")
        home.addWidget(self.home_note)
        self.content.addWidget(self.home_frame)

    def _build_overview(self):
        self.filter_frame = FilterPanel()
        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText(tr('请输入仓房名称'))
        self.keyword.returnPressed.connect(self.apply_filters)
        self.alarm_filter, self.state_filter = QComboBox(), QComboBox()
        for label, key in ((tr('全部'), "all"), (tr('有预警'), "any"), (tr('温度预警'), "temp"), (tr('湿度预警'), "humidity"), (tr('无预警'), "none")):
            self.alarm_filter.addItem(label, key)
        for label, key in ((tr('全部'), "all"), (tr('在线'), "online"), (tr('离线 / 过期'), "offline"), (tr('尚未采集'), "pending"), (tr('未配置分机'), "unconfigured")):
            self.state_filter.addItem(label, key)
        for title, widget in ((tr('仓房名称'), self.keyword), (tr('报警类型'), self.alarm_filter), (tr('分机状态'), self.state_filter)):
            self.filter_frame.add_field(title, widget)
        query, clear = QPushButton(tr('查询')), QPushButton(tr('清空'))
        query.setObjectName("primary")
        query.clicked.connect(self.apply_filters)
        clear.clicked.connect(self.clear_filters)
        self.filter_frame.add_action(query)
        self.filter_frame.add_action(clear)
        self.content.addWidget(self.filter_frame)

        self.list_frame = QFrame()
        self.list_frame.setObjectName("section")
        self.list_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        result = QVBoxLayout(self.list_frame)
        result.setContentsMargins(12, 10, 12, 12)
        result.setSpacing(8)
        heading = QHBoxLayout()
        heading.setSpacing(10)
        heading.addWidget(_label(tr('仓房'), "sectionTitle"))
        self.count_label = _label(tr('0 个仓房'), "sectionMeta")
        heading.addWidget(self.count_label)
        heading.addStretch()
        history = QPushButton(tr('历史记录'))
        history.setObjectName("secondary")
        history.clicked.connect(self.history_requested.emit)
        heading.addWidget(history)
        self.refresh_button = QPushButton(tr('刷新'))
        self.refresh_button.setObjectName("secondary")
        self.refresh_button.clicked.connect(self.request_refresh)
        heading.addWidget(self.refresh_button)
        self.view_combo = QComboBox()
        self.view_combo.addItem(tr('卡片展示'), "card")
        self.view_combo.addItem(tr('列表展示'), "list")
        self.view_combo.setMinimumWidth(110)
        self.view_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.view_combo.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.view_combo.currentIndexChanged.connect(self._render_rows)
        heading.addWidget(self.view_combo)
        result.addLayout(heading)
        divider = QFrame()
        divider.setObjectName("sectionDivider")
        divider.setFixedHeight(1)
        result.addWidget(divider)
        self.views = QStackedWidget()
        self.views.setObjectName("warehouseViews")
        self.card_area = QWidget()
        self.card_area.setObjectName("warehouseCardArea")
        self.card_grid = QGridLayout(self.card_area)
        self.card_grid.setContentsMargins(0, 0, 0, 0)
        self.card_grid.setSpacing(8)
        self.card_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.views.addWidget(self.card_area)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([tr('仓房名称'), tr('仓房温度'), tr('仓房湿度'), tr('报警类型'), tr('分机状态'), tr('更新时间'), tr('操作')])
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
        self.empty_label = _label(tr('尚无仓房。请先在仓房信息管理中添加仓房，并关联测温分机。'), "muted")
        result.addWidget(self.empty_label)
        self.pager = QWidget()
        pager = QHBoxLayout(self.pager)
        pager.setContentsMargins(0, 0, 0, 0)
        pager.addStretch()
        self.prev_button, self.next_button = QPushButton("‹"), QPushButton("›")
        self.prev_button.clicked.connect(lambda: self._turn_page(-1))
        self.next_button.clicked.connect(lambda: self._turn_page(1))
        self.page_label = _label("1 / 1", "muted")
        pager.addWidget(self.prev_button)
        pager.addWidget(self.page_label)
        pager.addWidget(self.next_button)
        result.addWidget(self.pager)
        self.content.addWidget(self.list_frame)

    def set_mode(self, mode: str):
        if mode not in ("home", "overview"):
            raise ValueError(tr('页面模式必须是 home 或 overview'))
        self.mode = mode
        self.title.setText(tr('首页') if mode == "home" else tr('粮情总览'))
        self.kicker.setText(tr('运行概况') if mode == "home" else tr('仓房粮情'))
        self.home_frame.setVisible(mode == "home")
        self.filter_frame.setVisible(mode == "overview")
        self.list_frame.setVisible(True)
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
            labels = {"temp": tr('温度预警'), "humidity": tr('湿度预警')}
            alarm_text = "、".join(labels[key] for key in labels if any(a.get("metric") == key for a in alarms)) or (tr('有预警') if alarms else tr('无预警'))
            if not self._alarms_loaded:
                alarm_text = tr('报警状态尚未读取')
            timestamps = [str(row.get("timestamp", "")) for identifier in ids for row in self.controller.latest.get(identifier, []) if row.get("timestamp")]
            state_label = {"online": tr('在线 {p0}/{p1}', p0=online, p1=len(devices)), "pending": tr('尚未采集'), "offline": tr('离线 / 数据过期'), "unconfigured": tr('未配置分机')}[state]
            rows.append({"id": warehouse["id"], "name": warehouse["name"], "devices": devices,
                         "first_device": devices[0]["id"] if devices else None,
                         "temperature": _number(temperature, "℃"), "humidity": _number(humidity, "%RH"),
                         "state": state, "state_label": state_label, "alarms": alarms, "alarm_text": alarm_text,
                         "updated": max(timestamps).replace("T", " ") if timestamps else ""})
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
        notes = ["", tr('共 {p0} 台', p0=len(metadata['devices'])), tr('共 {p0} 个测点', p0=grain_capacity), tr('需要确认') if stats["pending"] else ""]
        for label, text in zip(self.metric_notes, notes):
            label.setText(text)
            label.setToolTip(text)
        self.metric_values[1].setToolTip(tr('串口已打开且最近接收未过期；不等同于每个探头均正常。'))
        self.metric_values[2].setToolTip(tr('只统计有效粮温测点，故障和过期数据不计入。'))
        self.metric_values[3].setStyleSheet("color:#b44b3d;" if stats["pending"] else "")
        self.home_state.setText(tr('正在采集') if self.controller.running else tr('采集已暂停'))
        self.home_note.hide()
        keyword, alarm, state = self._filters if self.mode == "overview" else ("", "all", "all")
        self._rows = [row for row in rows if keyword.casefold() in row["name"].casefold()
                      and (state == "all" or row["state"] == state)
                      and (alarm == "all" or self._alarms_loaded and (alarm == "any" and row["alarms"]
                           or alarm == "none" and not row["alarms"]
                           or alarm in ("temp", "humidity") and any(a.get("metric") == alarm for a in row["alarms"])))]
        self.count_label.setText(tr('{p0} 个仓房', p0=len(self._rows)))
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
        self.pager.setVisible(page_count > 1)
        self.empty_label.setVisible(not selected)
        if not selected:
            self.empty_label.setText(tr('当前筛选条件下暂无仓房。') if self.controller.metadata["warehouses"] else tr('尚无仓房。请先在仓房信息管理中添加仓房，并关联测温分机。'))
        card_mode = self.view_combo.currentData() == "card"
        self.views.setCurrentIndex(0 if card_mode else 1)
        self.views.setVisible(bool(selected))
        if not card_mode:
            self.table.setRowCount(len(selected))
            for index, row in enumerate(selected):
                for column, value in enumerate((row["name"], row["temperature"], row["humidity"], row["alarm_text"], row["state_label"], row["updated"])):
                    self.table.setItem(index, column, QTableWidgetItem(str(value) if value else (tr("尚未接收") if column == 5 else "")))
                action = QPushButton(tr('查看粮情'))
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
        available_width = max(220, self.scroll.viewport().width() - 26)
        columns = min(4, max(1, (available_width + 8) // 248))
        column_width = min(280, (available_width - 8 * (columns - 1)) // columns)
        row_heights = {}
        for index, row in enumerate(selected):
            if row["id"] not in self._cards:
                card = _WarehouseCard()
                card.setObjectName("warehouseCard")
                card.setCursor(Qt.CursorShape.PointingHandCursor)
                card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
                content = QVBoxLayout(card)
                content.setContentsMargins(10, 8, 10, 8)
                content.setSpacing(4)
                labels = {key: _label("", "sectionTitle" if key == "name" else "muted") for key in ("name", "temperature", "humidity", "updated", "state_label", "alarm_text")}
                for key in ("temperature", "humidity"):
                    labels[key].setStyleSheet("font-size:19px; font-weight:600; color:#263a50;")
                labels["updated"].setObjectName("sectionMeta")
                labels["state_label"].setObjectName("sectionMeta")
                title_row = QHBoxLayout()
                title_row.addWidget(labels["name"], 1)
                title_row.addWidget(labels["state_label"])
                content.addLayout(title_row)
                environment = QHBoxLayout()
                environment.setSpacing(8)
                environment.addWidget(labels["temperature"])
                environment.addWidget(labels["humidity"])
                content.addLayout(environment)
                content.addWidget(labels["updated"])
                action = QPushButton(tr('详情 →'))
                action.setObjectName("quietButton")
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
            labels["temperature"].setText(row["temperature"])
            labels["temperature"].setToolTip(tr('仓温：有效仓温测点平均值'))
            labels["humidity"].setText(row["humidity"])
            labels["humidity"].setToolTip(tr('仓湿：有效仓湿测点平均值'))
            labels["updated"].setText(tr("更新 {time}", time=row["updated"][-8:]) if row["updated"] else tr("尚未接收"))
            labels["updated"].setToolTip(row["updated"])
            labels["state_label"].setText(row["state_label"])
            labels["alarm_text"].setText(row["alarm_text"])
            labels["alarm_text"].setStyleSheet("color:#b44b3d;" if row["alarms"] else "color:#738295;")
            action.setEnabled(row["first_device"] is not None)
            self.card_grid.addWidget(card, index // columns, index % columns, Qt.AlignmentFlag.AlignTop)
            card.setFixedWidth(column_width)
            card_height = card.layout().heightForWidth(column_width)
            card.setFixedHeight(max(118, card_height + 2))
            grid_row = index // columns
            row_heights[grid_row] = max(row_heights.get(grid_row, 0), card.height())
        for column in range(5):
            self.card_grid.setColumnMinimumWidth(column, 0)
            self.card_grid.setColumnStretch(column, 1 if column == columns else 0)
        self.card_grid.invalidate()
        # Newly inserted cards may not be shown yet, so Qt's cached layout hint
        # can still describe the previous page. Size all rows from their cards.
        height = sum(row_heights.values()) + max(0, len(row_heights) - 1) * self.card_grid.verticalSpacing()
        self.views.setFixedHeight(max(118, height))

    def _open_warehouse(self, identifier):
        row = next((row for row in self._rows if row["id"] == identifier), None)
        if row and row["first_device"] is not None:
            self.detail_requested.emit(row["first_device"])

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "views"):
            self._render_rows()

    def eventFilter(self, watched, event):
        if watched is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            # Reflow from the available viewport, not the old grid's minimum width.
            QTimer.singleShot(0, self._render_rows)
        return super().eventFilter(watched, event)

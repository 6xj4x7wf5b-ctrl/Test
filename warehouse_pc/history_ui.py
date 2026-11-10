"""Asynchronous paged history, a single-point trend, and full-range exports."""
from __future__ import annotations

from datetime import datetime
import math
import os
from pathlib import Path
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import QDateTime, Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDateTimeEdit, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from .filter_panel import FilterPanel
from .i18n import tr


_STATUS = {
    "ok": "正常", "unbound": "未绑定", "not_ready": "尚未采样", "offline": "离线",
    "crc_error": "CRC错误", "bus_error": "总线故障", "stale": "数据过期",
    "power_error": "供电模式错误", "timeout": "转换超时", "data_error": "数据格式无效",
    "unknown_255": "无有效数据", "unknown": "未知",
}
_KIND = {"grain": "粮温", "ambient": "仓温仓湿"}


def _number(value: Any) -> str:
    if value is None:
        return "—"
    try:
        value = float(value)
        return f"{value:.1f}" if math.isfinite(value) else "—"
    except (ValueError, TypeError):
        return "—"


class HistoryPage(QWidget):
    """Read-only history page; cleanup belongs to the separate maintenance UI.

    Public methods: ``refresh(metadata=None)``, ``query()``, ``set_view_mode(mode)``. Safe for
    repeated calls; generation tokens discard old repository callbacks.
    Selecting a row chooses that device/type/point for the trend only.
    """

    CURVE_LIMIT = 5000

    def __init__(self, repository, controller, parent=None):
        super().__init__(parent)
        self.repo, self.controller = repository, controller
        self._metadata: dict[str, Any] = {"devices": []}
        self._devices: dict[int, dict[str, Any]] = {}
        self._rows: list[dict[str, Any]] = []
        self._filters: dict[str, Any] | None = None
        self._applied_filters: dict[str, Any] | None = None
        self._view_mode = "records"
        self._query_version = self._plot_version = 0
        self._offset = self._total = 0
        self._count_loading = self._rows_loading = False
        self._plot_selection: tuple[int, str, int] | None = None
        self._plot_rows: list[dict[str, Any]] = []
        self._plot_total: int | None = None
        self._exporting = False
        self._disposed = False
        self.destroyed.connect(lambda: setattr(self, "_disposed", True))
        self._build_ui()
        if hasattr(controller, "metadata_changed"):
            controller.metadata_changed.connect(self.refresh)
        if hasattr(repository, "ready"):
            repository.ready.connect(lambda: self.refresh())
        QTimer.singleShot(0, lambda: self.refresh(getattr(controller, "metadata", None), query=False))

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        filters = FilterPanel()
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(130)
        self.device_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.device_combo.setMinimumContentsLength(8)
        self.device_combo.addItem(tr("全部分机"), None)
        self.kind_combo = QComboBox()
        self.kind_combo.addItem(tr("全部类型"), None)
        self.kind_combo.addItem(tr("粮温"), "grain")
        self.kind_combo.addItem(tr("仓温仓湿"), "ambient")
        self.point_spin = QSpinBox()
        self.point_spin.setRange(0, 165)
        self.point_spin.setSpecialValueText(tr("全部测点"))
        self.start_edit, self.end_edit = QDateTimeEdit(), QDateTimeEdit()
        for editor in (self.start_edit, self.end_edit):
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            editor.setMinimumWidth(156)
            editor.setToolTip(tr("按电脑接收时间查询，含起止时间"))
        now = QDateTime.currentDateTime()
        self.start_edit.setDateTime(now.addSecs(-24 * 3600))
        self.end_edit.setDateTime(now)
        self.query_button = QPushButton(tr("查询"))
        self.query_button.setObjectName("primary")
        self.query_button.clicked.connect(self.query)
        recent = QPushButton(tr("近 24 小时"))
        recent.setObjectName("secondary")
        recent.clicked.connect(self._recent)
        for label, widget in ((tr("分机"), self.device_combo), (tr("类型"), self.kind_combo), (tr("点号"), self.point_spin)):
            filters.add_field(label, widget)
        filters.add_field(tr("开始"), self.start_edit, row=1)
        filters.add_field(tr("结束"), self.end_edit, row=1)
        filters.add_action(recent, row=1)
        filters.add_action(self.query_button, row=1)
        layout.addWidget(filters)

        tools = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("muted")
        tools.addWidget(self.status_label, 1)
        self.export_label = QLabel()
        self.export_label.setObjectName("muted")
        self.export_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.export_label.setFixedWidth(144)
        tools.addWidget(self.export_label)
        self.export_button = QToolButton()
        self.export_button.setObjectName("secondary")
        self.export_button.setText(tr("导出"))
        self.export_button.setMinimumWidth(66)
        self.export_button.setToolTip(tr("导出上次查询的完整范围及每条记录的数据来源"))
        self.export_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        export_menu = QMenu(self.export_button)
        self.csv_button = export_menu.addAction("CSV…")
        self.excel_button = export_menu.addAction("Excel…")
        self.csv_button.triggered.connect(lambda checked=False: self.export("csv"))
        self.excel_button.triggered.connect(lambda checked=False: self.export("xlsx"))
        self.export_button.setMenu(export_menu)
        tools.addWidget(self.export_button)
        layout.addLayout(tools)

        self.table_area = table_area = QWidget()
        table_layout = QVBoxLayout(table_area)
        table_layout.setContentsMargins(0, 0, 0, 0)
        headers = [tr("接收时间"), tr("仓房"), tr("分机"), tr("类型"), tr("点号"), tr("原始温度 ℃"), tr("修正温度 ℃"),
                   tr("原始湿度 %RH"), tr("修正湿度 %RH"), tr("状态"), tr("来源"), tr("微调版本"), tr("绑定版本")]
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for index, width in enumerate((165, 95, 115, 95, 55, 95, 95, 110, 110, 105, 100, 80, 80)):
            self.table.setColumnWidth(index, width)
        self.table.itemSelectionChanged.connect(self._row_selected)
        self.table.setMinimumHeight(120)
        table_layout.addWidget(self.table, 1)
        pages = QHBoxLayout()
        self.previous_button, self.next_button = QPushButton(tr("上一页")), QPushButton(tr("下一页"))
        self.previous_button.clicked.connect(lambda: self._change_page(-1))
        self.next_button.clicked.connect(lambda: self._change_page(1))
        self.page_label = QLabel(tr("第 0 / 0 页"))
        self.page_size = QComboBox()
        for size in (100, 200, 500):
            self.page_size.addItem(tr('每页 {size} 条', size=size), size)
        self.page_size.setCurrentIndex(1)
        self.page_size.currentIndexChanged.connect(self._resize_page)
        pages.addWidget(self.previous_button)
        pages.addWidget(self.page_label)
        pages.addWidget(self.next_button)
        pages.addStretch()
        pages.addWidget(self.page_size)
        table_layout.addLayout(pages)
        layout.addWidget(table_area, 1)

        self.curve_area = curve_area = QFrame()
        curve_area.hide()
        curve_layout = QVBoxLayout(curve_area)
        curve_layout.setContentsMargins(0, 0, 0, 0)
        self.curve_label = QLabel(tr("选择测点后查询"))
        self.curve_label.setWordWrap(True)
        self.curve_label.setToolTip(tr("只绘制状态正常的读数，故障处断开。历史值不叠加当前微调。"))
        curve_layout.addWidget(self.curve_label)
        curves = QHBoxLayout()
        self.temperature_plot = self._plot(tr("修正温度"), "℃")
        self.humidity_plot = self._plot(tr("修正湿度"), "%RH")
        curves.addWidget(self.temperature_plot, 1)
        curves.addWidget(self.humidity_plot, 1)
        curve_layout.addLayout(curves, 1)
        layout.addWidget(curve_area, 1)
        self.device_combo.currentIndexChanged.connect(self._filter_changed)
        self.kind_combo.currentIndexChanged.connect(self._filter_changed)
        self.point_spin.valueChanged.connect(self._mark_edited)
        self.start_edit.dateTimeChanged.connect(self._mark_edited)
        self.end_edit.dateTimeChanged.connect(self._mark_edited)
        self._update_pagination()

    def set_view_mode(self, mode: str):
        if mode not in ("curve", "records"):
            raise ValueError(tr("历史视图必须为 curve 或 records"))
        if mode == self._view_mode:
            return
        self._view_mode = mode
        curve = mode == "curve"
        self.table_area.setVisible(not curve)
        self.curve_area.setVisible(curve)
        self._update_pagination()
        self._plot_version += 1
        if curve and self._filters is not None:
            if self._plot_selection is None and self._rows:
                self._select_plot(self._rows[0])
            else:
                self._load_plot()

    @staticmethod
    def _plot(label, units):
        plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        plot.setBackground("white")
        plot.showGrid(x=True, y=True, alpha=0.15)
        plot.setLabel("left", label, units=units, **{"font-size": "11px"})
        plot.setLabel("bottom", tr("电脑接收时间"), **{"font-size": "11px"})
        plot.addLegend(offset=(8, 4))
        plot.setMinimumHeight(110)
        tick_font = QFont(plot.font())
        tick_font.setPixelSize(11)
        for name in ("left", "bottom"):
            axis = plot.getAxis(name)
            axis.setPen("#b4c0cd")
            axis.setTextPen("#53667a")
            axis.setStyle(tickFont=tick_font)
        return plot

    def refresh(self, metadata=None, *, query=True):
        if self._disposed:
            return
        if not isinstance(metadata, dict):
            metadata = getattr(self.controller, "metadata", self._metadata)
        self._metadata = metadata or {"devices": []}
        self._devices = {int(row["id"]): row for row in self._metadata.get("devices", [])}
        previous = self.device_combo.currentData()
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        self.device_combo.addItem(tr("全部分机"), None)
        for identifier, device in self._devices.items():
            name = f"{device.get('warehouse_name', '')} · {device.get('name', tr('分机 {identifier}', identifier=identifier))}".strip(" ·")
            self.device_combo.addItem(name, identifier)
        index = self.device_combo.findData(previous)
        self.device_combo.setCurrentIndex(max(0, index))
        self.device_combo.blockSignals(False)
        self._filter_changed(mark=False)
        if query and self._filters is not None:
            self._load_page()

    def _filter_changed(self, *_args, mark=True):
        kind = self.kind_combo.currentData()
        # History can include formerly configured points; do not truncate its
        # selectable range to the device's present live-collection point count.
        maximum = 5 if kind == "ambient" else 165
        self.point_spin.setMaximum(maximum)
        if mark:
            self._mark_edited()

    def _mark_edited(self, *_args):
        if self._filters is not None:
            self.status_label.setText(tr("筛选条件已更改，点击“查询”更新；下方仍为上次查询结果。"))

    def _selected_filters(self):
        start, end = self.start_edit.dateTime(), self.end_edit.dateTime()
        if start > end:
            raise ValueError(tr("开始时间不能晚于结束时间"))
        return {"device_id": self.device_combo.currentData(),
                "start": start.toPython().isoformat(timespec="seconds"),
                "end": end.toPython().isoformat(timespec="seconds"),
                "kind": self.kind_combo.currentData(), "point": self.point_spin.value() or None}

    def _recent(self):
        now = QDateTime.currentDateTime()
        self.start_edit.setDateTime(now.addSecs(-24 * 3600))
        self.end_edit.setDateTime(now)
        self.query()

    def query(self, *_args):
        if self._disposed:
            return
        try:
            filters = self._selected_filters()
        except ValueError as error:
            self.status_label.setText(tr('无法查询：{error}', error=error))
            return
        self._filters = filters
        self._offset = 0
        self._plot_version += 1
        if all(filters[key] is not None for key in ("device_id", "kind", "point")):
            self._plot_selection = (filters["device_id"], filters["kind"], filters["point"])
        elif self._plot_selection is not None:
            device, kind, point = self._plot_selection
            if any(filters[key] is not None and filters[key] != value for key, value in
                   (("device_id", device), ("kind", kind), ("point", point))):
                self._plot_selection = None
        self._load_page()
        if self._plot_selection and self._view_mode == "curve":
            self._load_plot()
        else:
            self._clear_plot(tr("指定分机、类型和点号；未指定时显示第一条匹配记录所属测点。"))

    def _valid_query(self, version):
        return not self._disposed and self._query_version == version

    def _load_page(self):
        if self._filters is None:
            return
        self._query_version += 1
        version, offset, size = self._query_version, self._offset, self.page_size.currentData()
        filters = self._filters.copy()
        self._count_loading = self._rows_loading = True
        self._applied_filters = None
        self.status_label.setText(tr("正在后台查询历史记录…"))
        self._update_pagination()

        def failed(error):
            if self._valid_query(version):
                self._query_version += 1
                self._count_loading = self._rows_loading = False
                self._rows, self._total, self._offset = [], 0, 0
                self._filters = None
                self.table.setRowCount(0)
                self._update_pagination()
                self.status_label.setText(tr('历史查询失败：{error}', error=error))

        def counted(total):
            if not self._valid_query(version):
                return
            self._total = int(total)
            self._count_loading = False
            if self._total and offset >= self._total:
                self._offset = (self._total - 1) // size * size
                self._load_page()
                return
            if not self._total and offset:
                self._offset = 0
                self._load_page()
                return
            finish()

        def loaded(rows):
            if not self._valid_query(version):
                return
            self._rows = list(rows or [])
            self._rows_loading = False
            self._show_rows()
            if self._view_mode == "curve" and self._plot_selection is None and self._rows:
                self._select_plot(self._rows[0])
            finish()

        def finish():
            if not self._count_loading and not self._rows_loading:
                self._applied_filters = filters.copy()
            self._update_pagination()

        self.repo.call("history_count_filters", **filters, callback=counted, on_error=failed)
        self.repo.call("history_page", **filters, limit=size, offset=offset, callback=loaded, on_error=failed)

    def _show_rows(self):
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            source = tr("模拟数据") if row.get("source") == "simulator" else tr("真实设备")
            values = [str(row.get("timestamp", "")).replace("T", " "), row.get("warehouse_name", ""),
                      row.get("device_name", tr('分机 {identifier}', identifier=row.get('device_id', ''))),
                      tr(_KIND.get(row.get("kind"), row.get("kind", ""))), row.get("point", ""),
                      _number(row.get("raw_temp")), _number(row.get("temp")),
                      _number(row.get("raw_humidity")), _number(row.get("humidity")),
                      tr(_STATUS.get(row.get("status"), row.get("status", "未知"))), source,
                      row.get("calibration_version", 0), row.get("binding_version", 0)]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column >= 4 and column not in (9, 10):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if column == 10:
                    item.setForeground(QColor("#a36611" if row.get("source") == "simulator" else "#28624a"))
                if column == 9 and row.get("status") != "ok":
                    item.setForeground(QColor("#ab4d3f"))
                if column == 0:
                    item.setToolTip(tr("电脑接收时间；不代表传感器内部的日历采样时间"))
                self.table.setItem(index, column, item)
        self.table.blockSignals(False)

    def _update_pagination(self):
        size = self.page_size.currentData()
        pages = math.ceil(self._total / size) if self._total else 0
        current = self._offset // size + 1 if self._total else 0
        loading = self._count_loading or self._rows_loading
        self.page_label.setText(tr('第 {current} / {pages} 页', current=current, pages=pages))
        self.previous_button.setEnabled(not loading and self._offset > 0)
        self.next_button.setEnabled(not loading and self._offset + size < self._total)
        self._update_export_buttons()
        if not loading and self._filters is not None:
            if self._rows:
                text = tr('共 {count:,} 条', count=self._total)
                if self._view_mode == "records":
                    text += tr(' · 当前显示第 {first:,}～{last:,} 条', first=self._offset + 1, last=self._offset + len(self._rows))
            else:
                text = tr("所选范围没有历史记录。")
            try:
                edited = self._selected_filters() != self._filters
            except ValueError:
                edited = True
            self.status_label.setText(text + (tr(" · 筛选已修改，点击“查询”应用") if edited else ""))

    def _change_page(self, direction):
        if self._filters is None or self._count_loading or self._rows_loading:
            return
        size = self.page_size.currentData()
        offset = self._offset + direction * size
        if 0 <= offset < self._total:
            self._offset = offset
            self._load_page()

    def _resize_page(self, *_):
        if self._filters is not None:
            self._offset = 0
            self._load_page()

    def _row_selected(self):
        index = self.table.currentRow()
        if not self.table.selectedItems() or not 0 <= index < len(self._rows):
            return
        self._select_plot(self._rows[index])

    def _select_plot(self, row):
        self._plot_selection = (int(row["device_id"]), row["kind"], int(row["point"]))
        if self._view_mode == "curve":
            self._load_plot()

    def _clear_plot(self, note):
        self.temperature_plot.clear()
        self.humidity_plot.clear()
        self.humidity_plot.setVisible(True)
        self.curve_label.setText(note)

    def _load_plot(self):
        if self._filters is None or self._plot_selection is None:
            return
        self._plot_version += 1
        version = self._plot_version
        device, kind, point = self._plot_selection
        filters = self._filters.copy()
        filters.update(device_id=device, kind=kind, point=point)
        self._plot_rows, self._plot_total = [], None
        name = self._devices.get(device, {}).get("name", tr('分机 {identifier}', identifier=device))
        title = tr("{name} · {kind} {point} 号点", name=name, kind=tr(_KIND.get(kind, kind)), point=point)
        self._clear_plot(tr('{title} · 正在后台读取…', title=title))
        self.humidity_plot.setVisible(kind == "ambient")

        def current():
            return not self._disposed and self._plot_version == version

        def failed(error):
            if current():
                self._plot_version += 1
                self._clear_plot(tr('{title} · 曲线读取失败：{error}', title=title, error=error))

        def caption():
            if not current():
                return
            total = tr('共 {count:,} 条', count=self._plot_total) if self._plot_total is not None else tr("正在统计总数")
            if (self._plot_total or 0) > self.CURVE_LIMIT:
                limited = tr("仅绘最近 {limit:,} 条，完整数据请导出", limit=self.CURVE_LIMIT)
            else:
                limited = tr("绘制 {count:,} 条范围", count=len(self._plot_rows))
            period = ""
            if self._plot_rows:
                stamps = sorted(str(row.get("timestamp", "")).replace("T", " ") for row in self._plot_rows)
                period = f" · {stamps[0]} ～ {stamps[-1]}"
            valid = sum(row.get("status") == "ok" and row.get("temp") is not None for row in self._plot_rows)
            self.curve_label.setText(tr("{title} · {total} · {limited} · {valid:,} 条有效值{period}",
                                        title=title, total=total, limited=limited, valid=valid, period=period))

        def counted(total):
            if current():
                self._plot_total = int(total)
                caption()

        def loaded(rows):
            if not current():
                return
            self._plot_rows = list(rows or [])
            self._draw_curve(self._plot_rows, kind)
            caption()

        self.repo.call("history_count_filters", **filters, callback=counted, on_error=failed)
        self.repo.call("history_page", **filters, limit=self.CURVE_LIMIT, offset=0, callback=loaded, on_error=failed)

    def _draw_curve(self, rows, kind):
        self.temperature_plot.clear()
        self.humidity_plot.clear()
        rows = sorted(rows, key=lambda row: (str(row.get("timestamp", "")), int(row.get("id") or 0)))
        for source, source_name, color, style in (("serial", tr("真实设备"), "#28624a", Qt.PenStyle.SolidLine),
                                                   ("simulator", tr("模拟数据"), "#b87917", Qt.PenStyle.DashLine)):
            if not any(row.get("source") == source for row in rows):
                continue
            xs, temperatures, humidities = [], [], []
            for row in rows:
                try:
                    timestamp = datetime.fromisoformat(str(row["timestamp"])).timestamp()
                except (ValueError, TypeError, KeyError):
                    continue
                xs.append(timestamp)
                valid = row.get("source") == source and row.get("status") == "ok"
                for field, output in (("temp", temperatures), ("humidity", humidities)):
                    try:
                        value = float(row[field]) if valid and row.get(field) is not None else math.nan
                    except (ValueError, TypeError):
                        value = math.nan
                    output.append(value if math.isfinite(value) else math.nan)
            options = {"pen": pg.mkPen(color, width=2, style=style), "name": source_name,
                       "connect": "finite", "symbol": "o" if len(xs) == 1 else None, "symbolSize": 6}
            self.temperature_plot.plot(xs, temperatures, **options)
            if kind == "ambient":
                self.humidity_plot.plot(xs, humidities, **options)
        self.temperature_plot.enableAutoRange()
        self.humidity_plot.enableAutoRange()

    def export(self, suffix="csv"):
        if self._exporting or self._disposed:
            return
        if self._applied_filters is None:
            self._export_feedback(tr("请先查询"), tr("请先完成查询，再导出已应用的筛选范围。"))
            return
        filters = self._applied_filters.copy()
        suffix = "xlsx" if suffix == "xlsx" else "csv"
        title = tr("导出完整筛选范围为 Excel") if suffix == "xlsx" else tr("导出完整筛选范围为 CSV")
        file_filter = tr("Excel 工作簿 (*.xlsx)") if suffix == "xlsx" else tr("CSV 数据 (*.csv)")
        default = tr('历史数据_{stamp:%Y%m%d_%H%M%S}.{suffix}', stamp=datetime.now(), suffix=suffix)
        destination, _ = QFileDialog.getSaveFileName(
            self, title, default, file_filter, options=QFileDialog.Option.DontConfirmOverwrite)
        if not destination:
            return
        path = Path(destination)
        if path.suffix.lower() != f".{suffix}":
            path = path.with_suffix(f".{suffix}")
        if os.path.lexists(path):
            answer = QMessageBox.question(
                self, tr("确认覆盖文件"), tr('目标文件已存在：{path}\n是否覆盖？', path=path),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._exporting = True
        self._update_export_buttons()
        self._export_feedback(tr("正在导出…"), tr("正在后台导出上次查询的完整范围。"))

        def done(count):
            if self._disposed:
                return
            self._export_finished()
            self._export_feedback(tr('已导出 {count:,} 条', count=int(count)), str(path))

        def failed(error):
            if self._disposed:
                return
            self._export_finished()
            self._export_feedback(tr("导出失败"), tr('历史导出失败：{error}', error=error))

        self.repo.call("export_history", str(path), **filters, callback=done, on_error=failed)

    def _export_feedback(self, text, details):
        self.export_label.setText(text)
        self.export_label.setToolTip(details)
        self.export_label.setAccessibleDescription(details)

    def _export_finished(self):
        self._exporting = False
        self._update_export_buttons()

    def _update_export_buttons(self):
        enabled = self._applied_filters is not None and not self._exporting
        self.export_button.setEnabled(enabled)
        self.csv_button.setEnabled(enabled)
        self.excel_button.setEnabled(enabled)

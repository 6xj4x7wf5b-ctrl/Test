"""Manual GXHT3W registration against the firmware maintenance interface.

The containing window pauses polling for this profile for the dialog lifetime.
Only explicit SAVE changes the active configuration. Never retry a write after
an ambiguous transport failure: read the device back first.
"""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .domain import rom_bytes
from .protocol import build_maintenance


RESULT_NAMES = {
    1: "参数超出分机容量", 2: "分机不支持该参数或操作", 3: "传感器或测点重复绑定",
    4: "分机正忙，请等待当前操作结束", 5: "分机配置存储失败",
    6: "单总线通信或扫描失败", 7: "未找到该测点",
}
MAX_BINDINGS = 4096


def _integer(value: Any, name: str, lower: int, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ValueError(f"{name}必须在 {lower}～{upper} 范围内")
    return value


def normalise_bindings(records: Any, info: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        raise ValueError("绑定表必须是列表")
    if len(records) > info["grain_capacity"] + info["ambient_capacity"]:
        raise ValueError("绑定数量超过目标分机容量")
    result, points, roms = [], set(), set()
    for item in records:
        if not isinstance(item, dict) or item.get("kind") not in ("grain", "ambient"):
            raise ValueError("绑定用途必须是 grain 或 ambient")
        kind = item["kind"]
        point = _integer(item.get("point"), "测点编号", 1, info[f"{kind}_capacity"])
        bus = _integer(item.get("bus"), "支路编号", 0, info["bus_count"] - 1)
        rom = rom_bytes(item.get("rom"))
        if (kind, point) in points or rom in roms:
            raise ValueError("绑定表含重复测点或重复 ROM")
        points.add((kind, point))
        roms.add(rom)
        result.append({"kind": kind, "point": point, "bus": bus, "rom": rom})
    return sorted(result, key=lambda row: (row["kind"] != "grain", row["point"]))


def validate_backup(document: Any, address: int, info: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate before issuing any mutating command. Address is not identity."""
    if not isinstance(document, dict) or document.get("format") != "warehouse-registration-v1":
        raise ValueError("不是受支持的探头登记备份文件")
    if document.get("table") != "active":
        raise ValueError("仅支持恢复完整的已保存活动表备份")
    board = document.get("board")
    if not isinstance(board, dict):
        raise ValueError("备份缺少来源分机资料")
    source_address = _integer(board.get("address"), "备份分机地址", 1, 247)
    if source_address != address:
        raise ValueError(f"备份地址 {source_address} 与目标地址 {address} 不同，已停止恢复")
    limits = document.get("capacity")
    if not isinstance(limits, dict):
        raise ValueError("备份缺少容量信息")
    for key in ("grain_capacity", "ambient_capacity", "bus_count"):
        _integer(limits.get(key), f"备份 {key}", 1, MAX_BINDINGS)
    _integer(document.get("generation"), "备份配置代次", 0, 0xFFFFFFFF)
    if not isinstance(document.get("simulated"), bool):
        raise ValueError("备份缺少明确的模拟/真实设备标记")
    rows = normalise_bindings(document.get("bindings"), info)
    count = _integer(document.get("binding_count"), "备份条目数", 0, MAX_BINDINGS)
    if count != len(rows):
        raise ValueError("备份条目数不完整或已被修改")
    # Also verify the manifest is internally valid for the source capacities.
    normalise_bindings(document["bindings"], limits)
    return rows


class RegistrationDialog(QDialog):
    # Emitted only after SAVE (including an ambiguous response reconciled by
    # read-back), never merely on staging or reading a table.
    bindings_saved = Signal(object)

    def __init__(self, manager: Any, profile_id: str, address: int,
                 device_name: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        _integer(address, "维护地址", 1, 247)
        self.manager, self.profile_id, self.address = manager, profile_id, address
        self.device_name = device_name or f"分机 {address}"
        self.info: dict[str, Any] | None = None
        self.dirty = False
        self._uncertain = False
        self._busy = False
        self._token, self._sequence = uuid.uuid4().hex, 0
        self._pending: dict[str, Any] | None = None
        self._cache: dict[int, list[dict[str, Any]]] = {}
        self._scan_records: list[dict[str, Any]] = []
        self._scan_started = 0.0
        self._recovering = False
        self._closed = False
        self._goal: list[dict[str, Any]] | None = None
        self._goal_table = 1
        self._last_error = ""
        self._simulator = "simulator" in profile_id.lower() or "模拟" in self.device_name
        if callable(getattr(manager, "is_simulator", None)):
            self._simulator = bool(manager.is_simulator(profile_id))
        self.setWindowTitle(f"探头登记 · {self.device_name} · 地址 {address}")
        self.resize(1000, 660)
        self._build_ui()
        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(300)
        self._scan_timer.timeout.connect(self._poll_scan)
        manager.response.connect(self._response)
        manager.failure.connect(self._failure)
        if hasattr(manager, "connection_changed"):
            manager.connection_changed.connect(self._connection_changed)
        self.finished.connect(self._disconnect)
        QTimer.singleShot(0, self.refresh)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        identity = QLabel(f"目标：{self.device_name}  ｜  串口配置：{self.profile_id}  ｜  分机地址：{self.address}")
        identity.setWordWrap(True)
        layout.addWidget(identity)
        self.simulator_label = QLabel("模拟数据 · 不代表真实设备或实际安装位置")
        self.simulator_label.setStyleSheet("color:#9a5800;font-weight:600;padding:6px;background:#fff3d9")
        self.simulator_label.setVisible(self._simulator)
        layout.addWidget(self.simulator_label)
        note = QLabel("扫描顺序不能确定安装位置。请逐个核实实物标签后绑定；绑定只修改暂存表，点击“保存并核对”后才生效。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.info_label = QLabel("正在读取分机信息…")
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)
        top = QHBoxLayout()
        self.refresh_button = QPushButton("刷新分机信息")
        self.refresh_button.clicked.connect(self.refresh)
        top.addWidget(self.refresh_button)
        top.addWidget(QLabel("扫描支路（从 0 起）"))
        self.scan_bus = QSpinBox()
        self.scan_bus.setRange(0, 0)
        top.addWidget(self.scan_bus)
        self.scan_button = QPushButton("扫描探头")
        self.scan_button.clicked.connect(self.start_scan)
        top.addWidget(self.scan_button)
        top.addStretch()
        layout.addLayout(top)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        left, right = QWidget(), QWidget()
        left_layout, right_layout = QVBoxLayout(left), QVBoxLayout(right)
        left_layout.addWidget(QLabel("本次完整扫描结果"))
        self.scan_table = self._table(["支路", "探头 ROM（原始字节顺序）"])
        self.scan_table.itemSelectionChanged.connect(self._select_probe)
        left_layout.addWidget(self.scan_table)
        form = QFormLayout()
        self.rom_field = QLineEdit()
        self.rom_field.setReadOnly(True)
        self.rom_field.setPlaceholderText("请先扫描并选择探头")
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("粮温", "grain")
        self.kind_combo.addItem("仓温仓湿", "ambient")
        self.kind_combo.currentIndexChanged.connect(self._point_range)
        self.point_spin, self.binding_bus = QSpinBox(), QSpinBox()
        self.point_spin.setRange(1, 1)
        self.binding_bus.setRange(0, 0)
        form.addRow("已选探头", self.rom_field)
        form.addRow("用途", self.kind_combo)
        form.addRow("测点编号", self.point_spin)
        form.addRow("支路编号", self.binding_bus)
        left_layout.addLayout(form)
        self.bind_button = QPushButton("绑定到暂存表")
        self.bind_button.clicked.connect(self.bind_probe)
        left_layout.addWidget(self.bind_button)
        table_top = QHBoxLayout()
        self.table_combo = QComboBox()
        self.table_combo.addItem("暂存表（待保存）", 1)
        self.table_combo.addItem("活动表（已保存）", 0)
        self.table_combo.currentIndexChanged.connect(self._table_changed)
        table_top.addWidget(self.table_combo)
        self.read_button = QPushButton("完整读取")
        self.read_button.clicked.connect(self.read_bindings)
        table_top.addWidget(self.read_button)
        right_layout.addLayout(table_top)
        self.binding_table = self._table(["用途", "编号", "支路", "探头 ROM"])
        right_layout.addWidget(self.binding_table)
        self.unbind_button = QPushButton("解绑选中测点（暂存）")
        self.unbind_button.clicked.connect(self.unbind_probe)
        right_layout.addWidget(self.unbind_button)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([460, 620])
        layout.addWidget(splitter, 1)
        bottom = QHBoxLayout()
        self.save_button, self.reload_button = QPushButton("保存并核对"), QPushButton("放弃暂存修改")
        self.backup_button, self.restore_button = QPushButton("备份活动表…"), QPushButton("从 JSON 恢复暂存表…")
        self.save_button.clicked.connect(self.save)
        self.reload_button.clicked.connect(self.reload)
        self.backup_button.clicked.connect(self.backup)
        self.restore_button.clicked.connect(self.restore)
        for button in (self.save_button, self.reload_button, self.backup_button, self.restore_button):
            bottom.addWidget(button)
        bottom.addStretch()
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.reject)
        bottom.addWidget(self.close_button)
        layout.addLayout(bottom)
        self.status_label = QLabel("准备登记")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self._set_busy(False)

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        widget = QTableWidget(0, len(headers))
        widget.setHorizontalHeaderLabels(headers)
        widget.verticalHeader().setDefaultSectionSize(28)
        widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        widget.horizontalHeader().setStretchLastSection(True)
        return widget

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        ready = self.info is not None and not busy
        for widget in (self.scan_button, self.read_button, self.bind_button, self.unbind_button,
                       self.save_button, self.reload_button, self.backup_button,
                       self.restore_button, self.table_combo, self.scan_bus, self.kind_combo,
                       self.point_spin, self.binding_bus):
            widget.setEnabled(ready)
        self.refresh_button.setEnabled(not busy)
        self.close_button.setEnabled(not busy)
        if self.info and self.info["scan_busy"]:
            for button in (self.scan_button, self.bind_button, self.unbind_button,
                           self.save_button, self.reload_button, self.restore_button):
                button.setEnabled(False)

    def _finish(self, text: str) -> None:
        self._scan_timer.stop()
        self._goal = None
        self._set_busy(False)
        self.status_label.setText(text)

    def _error(self, message: str) -> None:
        if self._recovering:
            self._recovering = False
            self._uncertain = True
        self._last_error = message
        self._finish(message)
        QMessageBox.warning(self, "操作未完成", message)

    def _request(self, op: int, payload: bytes, callback: Callable[[dict], None],
                 *, allow: tuple[int, ...] = (0,), write: bool = False) -> None:
        if self._pending is not None:
            raise RuntimeError("已有维护请求等待响应")
        self._sequence += 1
        context = {"registration": self._token, "sequence": self._sequence}
        if write:
            self._uncertain = True
            self.dirty = True
        self._pending = {"context": context, "op": op, "callback": callback,
                         "allow": allow, "write": write}
        try:
            self.manager.submit(self.profile_id, build_maintenance(self.address, op, payload),
                                {"protocol": "maintenance", "address": self.address, "op": op},
                                context=context)
        except Exception as exc:
            self._failure(self.profile_id, str(exc))

    def _response(self, profile_id: str, envelope: object, raw: bytes) -> None:
        if profile_id != self.profile_id or not isinstance(envelope, dict) or not self._pending:
            return
        pending = self._pending
        if envelope.get("context") != pending["context"]:
            return
        self._pending = None
        parsed = envelope.get("parsed")
        try:
            if not isinstance(parsed, dict) or parsed.get("protocol") != "maintenance" or \
                    parsed.get("address") != self.address or parsed.get("op") != pending["op"]:
                raise ValueError("维护响应与本次请求不匹配")
            result = parsed.get("result")
            if result not in pending["allow"]:
                raise ValueError(RESULT_NAMES.get(result, f"未知分机结果码：{result}"))
            pending["callback"](parsed)
        except (ValueError, KeyError, TypeError, RuntimeError) as exc:
            if pending["write"]:
                self._recover_write(f"写操作未能确认：{exc}")
            else:
                self._error(str(exc))

    def _failure(self, profile_id: str, message: str) -> None:
        if profile_id != self.profile_id or self._pending is None:
            return
        pending, self._pending = self._pending, None
        if pending["write"]:
            self._recover_write(f"写操作响应超时或通信中断：{message}")
        else:
            if self._recovering:
                self._recovering = False
                self._uncertain = True
                self.dirty = True
                self._error(f"配置结果仍不确定，未重发任何写命令。请检查连接后重新读取配置。\n{message}")
            else:
                self._error(f"通信失败，操作未完成：{message}")

    def _connection_changed(self, profile_id: str, connected: bool, message: str) -> None:
        if profile_id == self.profile_id and not connected and self._busy:
            self._pending = None
            self._error("连接已断开，当前操作未完成。写操作可能已生效，请重连后完整读取配置核对。\n" + message)

    def _get_info(self, callback: Callable[[], None]) -> None:
        def done(parsed: dict) -> None:
            value = parsed["info"]
            if value.get("version") != 1:
                raise ValueError("分机维护接口版本不受支持")
            for name in ("grain_capacity", "ambient_capacity", "bus_count"):
                _integer(value.get(name), name, 1, MAX_BINDINGS)
            if value["bus_count"] > 256 or value["grain_capacity"] + value["ambient_capacity"] > MAX_BINDINGS:
                raise ValueError("分机返回的容量超出登记工具支持范围")
            total_capacity = value["grain_capacity"] + value["ambient_capacity"]
            _integer(value.get("active_count"), "活动绑定数", 0, total_capacity)
            _integer(value.get("discovered_count"), "已发现数", 0, total_capacity)
            _integer(value.get("generation"), "配置代次", 0, 0xFFFFFFFF)
            self.info = dict(value)
            self.dirty = bool(value["dirty"])
            self.scan_bus.setMaximum(value["bus_count"] - 1)
            self.binding_bus.setMaximum(value["bus_count"] - 1)
            self._point_range()
            self.info_label.setText(
                f"接口 V{value['version']} ｜ 配置{'有效' if value['config_valid'] else '未保存'}"
                f" ｜ 暂存{'有修改' if self.dirty else '无修改'} ｜ 已保存 {value['active_count']} 点"
                f" ｜ 代次 {value['generation']} ｜ 粮温 {value['grain_capacity']} 点"
                f" / 仓温仓湿 {value['ambient_capacity']} 点 / {value['bus_count']} 支路")
            callback()
        self._request(0, b"", done)

    def refresh(self) -> None:
        if self._busy or self._closed:
            return
        self._set_busy(True)
        self.status_label.setText("读取分机信息与绑定表…")
        self._get_info(lambda: self._read_table(self.table_combo.currentData(),
                       lambda rows: self._finish(f"已完整读取 {len(rows)} 条绑定；扫描状态码 {self.info['scan_status']}")))

    def _point_range(self, *_: Any) -> None:
        if self.info:
            self.point_spin.setMaximum(self.info[f"{self.kind_combo.currentData()}_capacity"])

    def _table_changed(self, *_: Any) -> None:
        self._show_bindings(self._cache.get(self.table_combo.currentData(), []))

    def _show_bindings(self, rows: list[dict[str, Any]]) -> None:
        self.binding_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = ("粮温" if row["kind"] == "grain" else "仓温仓湿", row["point"],
                      row["bus"], row["rom"].hex(" ").upper())
            for column, value in enumerate(values):
                self.binding_table.setItem(index, column, QTableWidgetItem(str(value)))

    def _read_table(self, table: int, callback: Callable[[list[dict]], None]) -> None:
        assert self.info is not None
        keys = [(kind, point) for kind in ("grain", "ambient")
                for point in range(1, self.info[f"{kind}_capacity"] + 1)]
        rows: list[dict] = []
        self._cache.pop(table, None)

        def next_point(index: int) -> None:
            if index == len(keys):
                checked = normalise_bindings(rows, self.info)
                if table == 0 and len(checked) != self.info["active_count"]:
                    raise ValueError("完整读取的活动表数量与 INFO 不一致，请重新读取")
                self._cache[table] = checked
                if self.table_combo.currentData() == table:
                    self._show_bindings(checked)
                callback(checked)
                return
            kind, point = keys[index]
            self.status_label.setText(f"读取{'暂存' if table else '活动'}表 {index + 1}/{len(keys)}…")

            def got(parsed: dict) -> None:
                if parsed["result"] == 0:
                    row = parsed["binding"]
                    if row["kind"] != kind or row["point"] != point or row["table"] != table:
                        raise ValueError("分机返回了其他测点的绑定，已停止读取")
                    rows.append({key: row[key] for key in ("kind", "point", "bus", "rom")})
                next_point(index + 1)
            self._request(7, bytes([1 if kind == "grain" else 2]) + point.to_bytes(2, "big") + bytes([table]),
                          got, allow=(0, 7))
        next_point(0)

    def read_bindings(self) -> None:
        if self._busy or not self.info:
            return
        self._set_busy(True)
        self._get_info(lambda: self._read_table(self.table_combo.currentData(),
                       lambda rows: self._finish(f"完整读取完成：{len(rows)} 条绑定")))

    def start_scan(self) -> None:
        if self._busy or not self.info:
            return
        self._set_busy(True)
        self._scan_records = []
        self.scan_table.setRowCount(0)
        self.rom_field.clear()
        self._scan_started = time.monotonic()
        self.status_label.setText("请求扫描，等待分机完整搜索结束…")
        self._request(1, bytes([self.scan_bus.value()]), self._scan_accepted)

    def _scan_accepted(self, parsed: dict) -> None:
        self._scan_timer.start()
        self._poll_scan()

    def _poll_scan(self) -> None:
        if self._pending is not None:
            return
        if time.monotonic() - self._scan_started > 60:
            self._error("等待扫描结束超时。本次结果未作为完整扫描使用，请刷新分机信息。")
            return

        def checked() -> None:
            assert self.info is not None
            if self.info["scan_busy"]:
                self.status_label.setText(f"扫描中，分机已发现 {self.info['discovered_count']} 个探头…")
                return
            self._scan_timer.stop()
            if self.info["scan_status"] != 0:
                raise ValueError(f"扫描未完整成功：{RESULT_NAMES.get(self.info['scan_status'], '未知错误')}。未使用部分结果。")
            self._scan_page(0, self.info["discovered_count"])
        self._get_info(checked)

    def _scan_page(self, offset: int, total: int) -> None:
        assert self.info is not None
        if total > self.info["grain_capacity"] + self.info["ambient_capacity"]:
            raise ValueError("发现探头数超过分机容量，未使用本次扫描")

        def got(parsed: dict) -> None:
            records = parsed["records"]
            if parsed["total"] != total or parsed["offset"] != offset or len(records) > 27 or \
                    offset + len(records) > total or (offset < total and not records):
                raise ValueError("扫描分页数量不完整或前后不一致，已停止登记")
            for record in records:
                bus = _integer(record["bus"], "扫描支路", 0, self.info["bus_count"] - 1)
                rom = rom_bytes(record["rom"])
                if bus != self.scan_bus.value() or any(row["rom"] == rom for row in self._scan_records):
                    raise ValueError("扫描结果出现重复 ROM 或支路不匹配")
                self._scan_records.append({"bus": bus, "rom": rom})
            if len(self._scan_records) < total:
                self._scan_page(len(self._scan_records), total)
                return
            self.scan_table.setRowCount(total)
            for index, row in enumerate(self._scan_records):
                self.scan_table.setItem(index, 0, QTableWidgetItem(str(row["bus"])))
                self.scan_table.setItem(index, 1, QTableWidgetItem(row["rom"].hex(" ").upper()))
            self._finish(f"完整扫描成功：{total} 个探头。请根据实物位置人工指定测点编号。")
        self._request(2, offset.to_bytes(2, "big") + b"\x1b", got)

    def _select_probe(self) -> None:
        index = self.scan_table.currentRow()
        if 0 <= index < len(self._scan_records):
            row = self._scan_records[index]
            self.rom_field.setText(row["rom"].hex(" ").upper())
            self.binding_bus.setValue(row["bus"])

    def _verify_staged(self, text: str) -> None:
        goal = self._goal

        def read(rows: list[dict]) -> None:
            if goal is not None and rows != goal:
                raise ValueError("暂存表回读与目标不一致，请完整读取并核对；活动表尚未切换")
            def confirmed() -> None:
                self._uncertain = False
                self._finish(text + "；需点击保存才能生效")
            self._get_info(confirmed)
        self._read_table(1, read)

    def bind_probe(self) -> None:
        if self._busy or not self.info:
            return
        try:
            rom = rom_bytes(self.rom_field.text())
        except ValueError as exc:
            self._error(str(exc))
            return
        kind, point, bus = self.kind_combo.currentData(), self.point_spin.value(), self.binding_bus.value()
        selected = next((row for row in self._scan_records if row["rom"] == rom), None)
        if selected is None or selected["bus"] != bus:
            self._error("请选择本次完整扫描到的探头，并使用它实际所属的支路")
            return
        self._set_busy(True)

        def read(rows: list[dict]) -> None:
            old = next((row for row in rows if row["kind"] == kind and row["point"] == point), None)
            if old and old["rom"] != rom and QMessageBox.question(
                    self, "更换测点探头", f"将{'粮温' if kind == 'grain' else '仓温仓湿'} {point} 的探头\n"
                    f"{old['rom'].hex().upper()}\n更换为\n{rom.hex().upper()}？") != QMessageBox.StandardButton.Yes:
                self._finish("已取消绑定")
                return
            target = [row for row in rows if (row["kind"], row["point"]) != (kind, point)]
            target = normalise_bindings(target + [{"kind": kind, "point": point, "bus": bus, "rom": rom}], self.info)
            payload = bytes([1 if kind == "grain" else 2]) + point.to_bytes(2, "big") + bytes([bus]) + rom
            self._goal, self._goal_table = target, 1
            self._request(3, payload, lambda _: self._verify_staged("绑定已回读核对"), write=True)
        self._read_table(1, read)

    def unbind_probe(self) -> None:
        if self._busy or not self.info:
            return
        cached = self._cache.get(self.table_combo.currentData())
        index = self.binding_table.currentRow()
        if cached is None or not 0 <= index < len(cached):
            self._error("请先完整读取绑定表并选择要解绑的测点")
            return
        selected = cached[index]
        if QMessageBox.question(self, "确认解绑", f"从暂存表移除{'粮温' if selected['kind'] == 'grain' else '仓温仓湿'} "
                                f"{selected['point']}？保存前活动表保持当前配置。") != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True)

        def read(rows: list[dict]) -> None:
            self._goal = [row for row in rows if (row["kind"], row["point"]) != (selected["kind"], selected["point"])]
            self._goal_table = 1
            payload = bytes([1 if selected["kind"] == "grain" else 2]) + selected["point"].to_bytes(2, "big")
            self._request(4, payload, lambda _: self._verify_staged("解绑已回读核对"), write=True)
        self._read_table(1, read)

    def save(self) -> None:
        if self._busy or not self.info:
            return
        self._set_busy(True)

        def staged(rows: list[dict]) -> None:
            self._goal, self._goal_table = rows, 0

            def saved(_: dict) -> None:
                self._get_info(lambda: self._verify_saved(rows))
            self._request(5, b"", saved, write=True)
        self._read_table(1, staged)

    def _verify_saved(self, goal: list[dict]) -> None:
        if not self.info or not self.info["config_valid"] or self.info["dirty"]:
            raise ValueError("保存后的分机状态未确认，请重新读取核对")

        def done(rows: list[dict]) -> None:
            if rows != goal:
                raise ValueError("保存后的活动表与目标不一致，不能确认保存成功")
            self._uncertain = False
            self.bindings_saved.emit({"generation": self.info["generation"], "bindings": rows})
            self._finish(f"保存成功，已完整回读核对 {len(rows)} 条活动绑定（代次 {self.info['generation']}）")
        self._read_table(0, done)

    def reload(self) -> None:
        if self._busy:
            return
        if QMessageBox.question(self, "放弃暂存修改", "重新加载活动配置，放弃全部未保存的绑定修改？") != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True)

        def loaded(_: dict) -> None:
            def verified(rows: list[dict]) -> None:
                self._uncertain = False
                self._finish(f"已重新加载 {len(rows)} 条已保存绑定")
            self._get_info(lambda: self._read_table(1, verified))
        self._request(6, b"", loaded, write=True)

    def _recover_write(self, cause: str) -> None:
        self._scan_timer.stop()
        self._uncertain, self.dirty, self._recovering = True, True, True
        self.status_label.setText("写入结果未确认，正在只读核对配置；不会自动重发写命令…")
        goal, table = self._goal, self._goal_table

        def info_ready() -> None:
            def verified(rows: list[dict]) -> None:
                confirmed = goal is not None and rows == goal
                if table == 0:
                    confirmed = confirmed and bool(self.info["config_valid"]) and not self.info["dirty"]
                self._recovering = False
                self._uncertain = not confirmed
                if confirmed:
                    if table == 0:
                        self.bindings_saved.emit({"generation": self.info["generation"], "bindings": rows})
                    self._finish("原写请求未收到有效确认；只读回查已与目标一致。" +
                                 ("活动配置已核对。" if table == 0 else "暂存配置已核对，仍需保存。"))
                else:
                    self._error(cause + f"\n已完整回读{'活动' if table == 0 else '暂存'}表，请核对实际配置。未自动重发写命令。")
            self._read_table(table, verified)
        self._get_info(info_ready)

    def backup(self) -> None:
        if self._busy or not self.info:
            return
        self._set_busy(True)

        def info_ready() -> None:
            if not self.info["config_valid"]:
                raise ValueError("分机尚无有效活动配置，请先登记并保存")
            generation = self.info["generation"]

            def rows_ready(rows: list[dict]) -> None:
                def checked() -> None:
                    if self.info["generation"] != generation:
                        raise ValueError("备份读取期间配置代次变化，请重新备份")
                    document = {
                        "format": "warehouse-registration-v1", "table": "active",
                        "created_utc": datetime.now(timezone.utc).isoformat(),
                        "board": {"address": self.address, "name": self.device_name,
                                  "profile_id": self.profile_id, "identity_note": "地址不是硬件唯一标识"},
                        "simulated": self._simulator, "generation": generation,
                        "capacity": {key: self.info[key] for key in ("grain_capacity", "ambient_capacity", "bus_count")},
                        "binding_count": len(rows),
                        "bindings": [{**row, "rom": row["rom"].hex().upper()} for row in rows],
                    }
                    filename, _ = QFileDialog.getSaveFileName(self, "备份完整活动表", f"分机{self.address}_登记备份.json", "JSON (*.json)")
                    if not filename:
                        self._finish("已取消备份")
                        return
                    try:
                        destination = Path(filename)
                        temporary = destination.with_name(destination.name + ".tmp")
                        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
                        temporary.replace(destination)
                    except OSError as exc:
                        self._error(f"备份文件未写入成功：{exc}")
                        return
                    self._finish(f"已备份 {len(rows)} 条活动绑定：{filename}")
                self._get_info(checked)
            self._read_table(0, rows_ready)
        self._get_info(info_ready)

    def restore(self) -> None:
        if self._busy or not self.info:
            return
        filename, _ = QFileDialog.getOpenFileName(self, "选择登记备份", "", "JSON (*.json)")
        if not filename:
            return
        try:
            path = Path(filename)
            if path.stat().st_size > 2_000_000:
                raise ValueError("备份文件过大")
            document = json.loads(path.read_text(encoding="utf-8"))
            target = validate_backup(document, self.address, self.info)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self._error(f"备份校验失败，未修改分机：{exc}")
            return
        if bool(document.get("simulated")) != self._simulator:
            self._error("模拟备份与真实设备备份不能混用，未修改分机")
            return
        phrase = f"恢复地址{self.address}"
        typed, accepted = QInputDialog.getText(
            self, "核对目标分机后恢复", f"来源：{document['board'].get('name', '未命名')}，地址 {document['board']['address']}\n"
            f"目标：{self.device_name}，配置 {self.profile_id}，地址 {self.address}\n"
            f"将完整替换暂存表为 {len(target)} 条绑定，活动表需另行保存。\n"
            "同地址不能证明是同一块板，请核对实物及 ROM 安装位置。\n"
            f"确认后输入：{phrase}")
        if not accepted or typed.strip() != phrase:
            self.status_label.setText("已取消恢复，分机未修改")
            return
        self._set_busy(True)
        self._goal, self._goal_table = target, 1

        def info_ready() -> None:
            # Validate again against live capacities before changing anything.
            validate_backup(document, self.address, self.info)

            def current_ready(current: list[dict]) -> None:
                operations = [(4, bytes([1 if row["kind"] == "grain" else 2]) + row["point"].to_bytes(2, "big")) for row in current]
                operations += [(3, bytes([1 if row["kind"] == "grain" else 2]) + row["point"].to_bytes(2, "big") + bytes([row["bus"]]) + row["rom"]) for row in target]

                def step(index: int) -> None:
                    if index == len(operations):
                        self._verify_staged("备份已完整恢复到暂存表并回读核对")
                        return
                    self.status_label.setText(f"恢复暂存表 {index + 1}/{len(operations)}；活动表尚未切换…")
                    op, payload = operations[index]
                    self._request(op, payload, lambda _: step(index + 1), write=True)
                step(0)
            self._read_table(1, current_ready)
        self._get_info(info_ready)

    def reject(self) -> None:
        if self._busy:
            QMessageBox.information(self, "操作进行中", "请等待当前请求结束。写入结果未确认前不关闭登记窗口。")
            return
        if self.dirty or self._uncertain:
            answer = QMessageBox.question(self, "尚有未保存或未确认的配置",
                "分机暂存表可能有未保存的更改，关闭不会自动保存或放弃。\n仍要关闭登记窗口？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        super().reject()

    def closeEvent(self, event: Any) -> None:
        event.ignore()
        self.reject()

    def _disconnect(self, *_: Any) -> None:
        self._closed = True
        self._scan_timer.stop()
        connections = [(self.manager.response, self._response), (self.manager.failure, self._failure)]
        if hasattr(self.manager, "connection_changed"):
            connections.append((self.manager.connection_changed, self._connection_changed))
        for signal, slot in connections:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

"""Manual GXHT3W registration against the firmware maintenance interface.

The host pauses polling while this editor is open. Edits remain local until
the user saves; an ambiguous write is checked by reading the board back.
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
    QAbstractItemView, QComboBox, QDialog, QFileDialog,
    QHBoxLayout, QHeaderView, QLabel, QMessageBox, QProgressBar,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .domain import rom_bytes
from .i18n import tr
from .protocol import BINDING_PAGE_LIMIT, build_maintenance


RESULT_NAMES = {
    1: "参数超出分机容量", 2: "分机不支持该参数或操作", 3: "传感器或测点重复绑定",
    4: "分机正忙，请等待当前操作结束", 5: "分机配置存储失败",
    6: "单总线通信或扫描失败", 7: "未找到该测点",
}
MAX_BINDINGS = 4096


def _integer(value: Any, name: str, lower: int, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ValueError(tr("{name}必须在 {lower}～{upper} 范围内", name=name, lower=lower, upper=upper))
    return value


def normalise_bindings(records: Any, info: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        raise ValueError(tr("绑定表必须是列表"))
    if len(records) > info["grain_capacity"] + info["ambient_capacity"]:
        raise ValueError(tr("绑定数量超过目标分机容量"))
    result, points, roms = [], set(), set()
    for item in records:
        if not isinstance(item, dict) or item.get("kind") not in ("grain", "ambient"):
            raise ValueError(tr("绑定用途必须是 grain 或 ambient"))
        kind = item["kind"]
        point = _integer(item.get("point"), tr("测点编号"), 1, info[f"{kind}_capacity"])
        bus = _integer(item.get("bus"), tr("支路编号"), 0, info["bus_count"] - 1)
        rom = rom_bytes(item.get("rom"))
        if (kind, point) in points or rom in roms:
            raise ValueError(tr("登记表含重复测点或重复探头编码"))
        points.add((kind, point))
        roms.add(rom)
        result.append({"kind": kind, "point": point, "bus": bus, "rom": rom})
    return sorted(result, key=lambda row: (row["kind"] != "grain", row["point"]))


def validate_backup(document: Any, address: int, info: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate before issuing any mutating command. Address is not identity."""
    if not isinstance(document, dict) or document.get("format") != "warehouse-registration-v1":
        raise ValueError(tr("不是受支持的探头登记备份文件"))
    if document.get("table") != "active":
        raise ValueError(tr("仅支持恢复完整的已保存活动表备份"))
    board = document.get("board")
    if not isinstance(board, dict):
        raise ValueError(tr("备份缺少来源分机资料"))
    source_address = _integer(board.get("address"), tr("备份分机地址"), 1, 247)
    if source_address != address:
        raise ValueError(tr("备份地址 {backup_address} 与目标地址 {target} 不同，已停止恢复",
                            backup_address=source_address, target=address))
    limits = document.get("capacity")
    if not isinstance(limits, dict):
        raise ValueError(tr("备份缺少容量信息"))
    for key in ("grain_capacity", "ambient_capacity", "bus_count"):
        _integer(limits.get(key), tr("备份 {field}", field=key), 1, MAX_BINDINGS)
    _integer(document.get("generation"), tr("备份配置代次"), 0, 0xFFFFFFFF)
    if not isinstance(document.get("simulated"), bool):
        raise ValueError(tr("备份缺少明确的模拟/真实设备标记"))
    rows = normalise_bindings(document.get("bindings"), info)
    count = _integer(document.get("binding_count"), tr("备份条目数"), 0, MAX_BINDINGS)
    if count != len(rows):
        raise ValueError(tr("备份条目数不完整或已被修改"))
    # Also verify the manifest is internally valid for the source capacities.
    normalise_bindings(document["bindings"], limits)
    return rows


class RegistrationDialog(QDialog):
    # The host owns the pause for this serial profile and releases it on finished.
    bindings_loaded = Signal(object)
    bindings_saved = Signal(object)

    def __init__(self, manager: Any, profile_id: str, address: int,
                 device_name: str = "", parent: QWidget | None = None, *,
                 embedded: bool = False, port_name: str | None = None):
        super().__init__(parent)
        _integer(address, tr("维护地址"), 1, 247)
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        else:
            self.resize(900, 620)
        self.manager, self.profile_id, self.address = manager, profile_id, address
        self.device_name = device_name or tr("分机 {address}", address=address)
        self.port_name = port_name
        self.info: dict[str, Any] | None = None
        self._active: list[dict[str, Any]] | None = None
        self._draft: list[dict[str, Any]] = []
        self._base_generation: int | None = None
        self._paged: bool | None = None
        self._scan_started = 0.0
        self._table_keys: list[tuple[str, int]] = []
        self._token, self._sequence = uuid.uuid4().hex, 0
        self._pending: dict[str, Any] | None = None
        self._busy = self._cancel_requested = self._close_after_read = self._closed = False
        self._writing = self._save_sent = self._recovering = self._uncertain = False
        self._goal: list[dict[str, Any]] | None = None
        self._simulator = ("simulator" in profile_id.lower() or "模拟" in self.device_name)
        if callable(getattr(manager, "is_simulator", None)):
            self._simulator = bool(manager.is_simulator(profile_id))
        self.setWindowTitle(tr("探头登记 · {name} · 地址 {address}", name=self.device_name, address=address))
        self._build_ui()
        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(300)
        self._scan_timer.timeout.connect(self._poll_scan)
        manager.response.connect(self._response)
        self._failure_signal = getattr(manager, "request_failed", None)
        if self._failure_signal is None:
            self._failure_signal = manager.failure
            self._failure_handler = self._failure
        else:
            self._failure_handler = self._request_failed
        self._failure_signal.connect(self._failure_handler)
        if hasattr(manager, "connection_changed"):
            manager.connection_changed.connect(self._connection_changed)
        self.finished.connect(self._disconnect)
        QTimer.singleShot(0, self.refresh)

    @property
    def dirty(self) -> bool:
        return self._active is not None and self._draft != self._active

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        target = QHBoxLayout()
        title = QLabel(self.device_name)
        title.setObjectName("sectionTitle")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        target.addWidget(title)
        address = tr("地址 {address}", address=self.address)
        detail = QLabel(" · ".join(part for part in (self.port_name, address) if part))
        detail.setObjectName("muted")
        target.addWidget(detail)
        if self._simulator:
            simulated = QLabel(tr("模拟设备"))
            simulated.setObjectName("muted")
            target.addWidget(simulated)
        self.device_pending_label = QLabel(tr("分机有未保存修改"))
        self.device_pending_label.setObjectName("muted")
        target.addWidget(self.device_pending_label)
        target.addStretch()
        self.advanced_button = QPushButton(tr("高级选项"))
        self.advanced_button.setObjectName("quietButton")
        self.advanced_button.setCheckable(True)
        target.addWidget(self.advanced_button)
        layout.addLayout(target)

        self.advanced_panel = QWidget()
        advanced = QVBoxLayout(self.advanced_panel)
        advanced.setContentsMargins(0, 0, 0, 0)
        self.info_label = QLabel(tr("正在读取分机信息…"))
        self.info_label.setObjectName("muted")
        self.info_label.setWordWrap(True)
        advanced.addWidget(self.info_label)
        backup_actions = QHBoxLayout()
        self.refresh_button = QPushButton(tr("重新读取分机"))
        self.refresh_button.clicked.connect(self.refresh)
        backup_actions.addWidget(self.refresh_button)
        self.backup_button = QPushButton(tr("备份已保存登记…"))
        self.backup_button.clicked.connect(self.backup)
        backup_actions.addWidget(self.backup_button)
        self.restore_button = QPushButton(tr("导入登记备份…"))
        self.restore_button.clicked.connect(self.restore)
        backup_actions.addWidget(self.restore_button)
        backup_actions.addStretch()
        advanced.addLayout(backup_actions)
        self.advanced_button.toggled.connect(self.advanced_panel.setVisible)
        self.advanced_panel.hide()
        layout.addWidget(self.advanced_panel)

        self.steps_label = QLabel()
        self.steps_label.setObjectName("muted")
        self.steps_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.steps_label)
        scan_row = QHBoxLayout()
        self.scan_button = QPushButton(tr("扫描探头"))
        self.scan_button.clicked.connect(self.start_scan)
        self.scan_button.setToolTip(tr("扫描顺序不代表安装位置，请核对探头上的编码标签。"))
        scan_row.addWidget(self.scan_button)
        self.scan_bus_label = QLabel(tr("支路"))
        self.scan_bus = QSpinBox()
        self.scan_bus.setRange(0, 0)
        scan_row.addWidget(self.scan_bus_label)
        scan_row.addWidget(self.scan_bus)
        self.probe_combo = QComboBox()
        self.probe_combo.addItem(tr("选择探头"))
        self.probe_combo.setToolTip(tr("选择与实物标签一致的探头编码。"))
        self.probe_combo.currentIndexChanged.connect(self._update_controls)
        scan_row.addWidget(self.probe_combo, 1)
        layout.addLayout(scan_row)

        binding_row = QHBoxLayout()
        self.kind_combo = QComboBox()
        self.kind_combo.addItem(tr("粮温"), "grain")
        self.kind_combo.addItem(tr("仓温仓湿"), "ambient")
        self.kind_combo.currentIndexChanged.connect(self._point_range)
        binding_row.addWidget(self.kind_combo)
        binding_row.addWidget(QLabel(tr("测点")))
        self.point_spin = QSpinBox()
        self.point_spin.setRange(1, 1)
        binding_row.addWidget(self.point_spin)
        binding_row.addStretch()
        self.bind_button = QPushButton(tr("绑定"))
        self.bind_button.setToolTip(tr("添加或更换当前测点的探头。保存前只修改此表。"))
        self.bind_button.clicked.connect(self.bind_probe)
        binding_row.addWidget(self.bind_button)
        layout.addLayout(binding_row)

        self.summary_label = QLabel(tr("已保存 —"))
        self.summary_label.setObjectName("muted")
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.summary_label)
        self.binding_table = QTableWidget(0, 5)
        self.binding_table.setHorizontalHeaderLabels([tr("用途"), tr("测点"), tr("探头编码"), tr("状态"), tr("支路")])
        self.binding_table.verticalHeader().setDefaultSectionSize(27)
        self.binding_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.binding_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.binding_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.binding_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.binding_table.itemSelectionChanged.connect(self._update_controls)
        layout.addWidget(self.binding_table, 1)

        bottom = QHBoxLayout()
        self.unbind_button = QPushButton(tr("移除绑定"))
        self.unbind_button.setObjectName("quietButton")
        self.unbind_button.clicked.connect(self.unbind_probe)
        bottom.addWidget(self.unbind_button)
        self.reload_button = QPushButton(tr("撤销修改"))
        self.reload_button.setObjectName("quietButton")
        self.reload_button.setToolTip(tr("撤销全部未保存的本地修改。"))
        self.reload_button.clicked.connect(self.reload)
        bottom.addWidget(self.reload_button)
        bottom.addStretch()
        self.cancel_button = QPushButton(tr("取消读取"))
        self.cancel_button.clicked.connect(self.cancel_read)
        bottom.addWidget(self.cancel_button)
        self.save_button = QPushButton(tr("保存到分机"))
        self.save_button.setObjectName("primary")
        self.save_button.setToolTip(tr("提交本地修改，并回读分机确认保存结果。"))
        self.save_button.clicked.connect(self.save)
        bottom.addWidget(self.save_button)
        self.close_button = QPushButton(tr("关闭登记"))
        self.close_button.setObjectName("quietButton")
        self.close_button.clicked.connect(self.reject)
        bottom.addWidget(self.close_button)
        layout.addLayout(bottom)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(5)
        self.progress.hide()
        layout.addWidget(self.progress)
        status = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        status.addWidget(self.status_label, 1)
        self.retry_button = QPushButton(tr("重新读取"))
        self.retry_button.setObjectName("quietButton")
        self.retry_button.clicked.connect(self.refresh)
        self.retry_button.hide()
        status.addWidget(self.retry_button)
        layout.addLayout(status)
        self._update_controls()

    def _update_controls(self, *_: Any) -> None:
        online = not self._closed and self.info is not None
        ready = online and self._active is not None and not self._busy
        scan_idle = online and not self.info["scan_busy"]
        multiple = online and self.info["bus_count"] > 1
        self.device_pending_label.setVisible(online and self.info["dirty"] and not self._writing)
        self.scan_bus_label.setVisible(multiple)
        self.scan_bus.setVisible(multiple)
        self.binding_table.setColumnHidden(4, not multiple)
        self.scan_button.setEnabled(ready and scan_idle)
        self.scan_bus.setEnabled(not self._busy and scan_idle)
        self.refresh_button.setEnabled(not self._busy)
        self.cancel_button.setVisible(self._busy and not self._writing)
        self.cancel_button.setEnabled(not self._cancel_requested)
        self.close_button.setEnabled(not self._writing)
        self.probe_combo.setEnabled(ready)
        self.kind_combo.setEnabled(ready)
        self.point_spin.setEnabled(ready)
        self.bind_button.setEnabled(ready and self.probe_combo.currentData() is not None)
        self.save_button.setEnabled(ready and self.dirty and scan_idle and not self._uncertain)
        self.reload_button.setEnabled(ready and self.dirty)
        self.reload_button.setVisible(self.dirty and not self._busy)
        self.backup_button.setEnabled(ready and self.info["config_valid"])
        self.restore_button.setEnabled(ready)
        row = self.binding_table.currentRow()
        selected = self.binding_table.selectedItems() and 0 <= row < len(self._table_keys)
        self.unbind_button.setEnabled(ready and bool(selected))
        self.unbind_button.setVisible(bool(selected) and not self._busy)
        if selected:
            key = self._table_keys[row]
            in_draft = any(self._key(binding) == key for binding in self._draft)
            self.unbind_button.setText(tr("移除绑定") if in_draft else tr("撤销移除"))
        step = 2 if self.dirty else 1 if self.probe_combo.count() > 1 else 0
        steps = (tr("1 扫描"), tr("2 编辑"), tr("3 保存"))
        self.steps_label.setText("&ensp;→&ensp;".join(f"<b>{text}</b>" if i == step else text for i, text in enumerate(steps)))

    def _point_range(self, *_: Any) -> None:
        if self.info is not None:
            self.point_spin.setMaximum(self.info[f"{self.kind_combo.currentData()}_capacity"])

    @staticmethod
    def _key(row: dict[str, Any]) -> tuple[str, int]:
        return row["kind"], row["point"]

    def _show_bindings(self) -> None:
        active = {self._key(row): row for row in self._active or []}
        draft = {self._key(row): row for row in self._draft}
        selected_row = self.binding_table.currentRow()
        selected = self._table_keys[selected_row] if 0 <= selected_row < len(self._table_keys) else None
        self._table_keys = sorted(active.keys() | draft.keys(), key=lambda key: (key[0] != "grain", key[1]))
        self.binding_table.blockSignals(True)
        self.binding_table.clearSelection()
        self.binding_table.setRowCount(len(self._table_keys))
        changes = 0
        for index, key in enumerate(self._table_keys):
            old, new = active.get(key), draft.get(key)
            if new == old:
                state = tr("已保存")
            elif old is None:
                state = tr("待新增")
            elif new is None:
                state = tr("待删除")
            else:
                state = tr("待修改")
            changes += new != old
            binding = new or old
            values = (tr("粮温") if key[0] == "grain" else tr("仓温仓湿"), key[1],
                      binding["rom"].hex(" ").upper(), state, binding["bus"])
            for column, value in enumerate(values):
                self.binding_table.setItem(index, column, QTableWidgetItem(str(value)))
        if selected in self._table_keys:
            self.binding_table.selectRow(self._table_keys.index(selected))
        self.binding_table.blockSignals(False)
        if changes:
            summary = tr("已保存 {saved} 条 · 未保存 {pending} 项", saved=len(active), pending=changes)
        else:
            summary = tr("已保存 {count} 条", count=len(active))
        self.summary_label.setText(summary)
        self._update_controls()

    def _show_probes(self, rows: list[dict[str, Any]]) -> None:
        self.probe_combo.blockSignals(True)
        self.probe_combo.clear()
        self.probe_combo.addItem(tr("选择探头"))
        for row in rows:
            name = row["rom"].hex(" ").upper()
            if self.info["bus_count"] > 1:
                name = tr("支路 {bus} · {name}", bus=row['bus'], name=name)
            self.probe_combo.addItem(name, row)
        self.probe_combo.blockSignals(False)
        self._update_controls()

    def _status(self, text: str, completed: int = 0, total: int = 0, *, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))
        self.retry_button.setVisible(error and not self._busy)
        if self._busy:
            self.progress.setRange(0, total)
            self.progress.setValue(completed)

    def _start(self, text: str) -> None:
        self._busy = True
        self._cancel_requested = False
        self.progress.show()
        self._status(text)
        self._update_controls()

    def _finish(self, text: str, *, error: bool = False) -> None:
        self._scan_timer.stop()
        self._goal = None
        self._busy = self._writing = self._save_sent = self._recovering = self._cancel_requested = False
        self.progress.hide()
        if self._uncertain and not error:
            text = "\n".join(part for part in (text, tr("分机保存结果尚未确认，请重新读取。")) if part)
            error = True
        self._status(text, error=error)
        self._update_controls()

    def _error(self, message: str) -> None:
        message = tr(message)
        if self._recovering:
            self._uncertain = True
            message = tr("暂未确认分机保存结果。请重新读取分机；没有重发写请求。\n{error}", error=message)
        self._finish(message, error=True)
        QMessageBox.warning(self, tr("操作未完成"), message)

    def cancel_read(self) -> None:
        if not self._busy or self._writing:
            return
        self._scan_timer.stop()
        self._cancel_requested = True
        if self._pending is None:
            self._read_cancelled()
        else:
            self._status(tr("正在取消，等待当前串口请求结束…"))
            self._update_controls()

    def _read_cancelled(self) -> None:
        close, self._close_after_read = self._close_after_read, False
        self._finish(tr("已取消读取，本地编辑保留。"))
        if close:
            self.reject()

    def _request(self, op: int, payload: bytes, callback: Callable[[dict], None], *,
                 allow: tuple[int, ...] = (0,), expected: dict[str, Any] | None = None) -> None:
        if self._pending is not None:
            raise RuntimeError(tr("已有维护请求等待响应"))
        self._sequence += 1
        context = {"registration": self._token, "sequence": self._sequence}
        self._pending = {"context": context, "op": op, "callback": callback, "allow": allow}
        if op in (3, 4, 5) and not self._writing:
            self._writing = self._uncertain = True
            self._update_controls()
        try:
            match = {"protocol": "maintenance", "address": self.address, "op": op}
            if expected:
                match.update(expected)
            self.manager.submit(self.profile_id, build_maintenance(self.address, op, payload), match, context=context)
        except Exception as exc:
            self._failure(self.profile_id, str(exc))

    def _response(self, profile_id: str, envelope: object, raw: bytes) -> None:
        if profile_id != self.profile_id or not isinstance(envelope, dict) or self._pending is None:
            return
        pending = self._pending
        if envelope.get("context") != pending["context"]:
            return
        self._pending = None
        if self._cancel_requested:
            self._read_cancelled()
            return
        parsed = envelope.get("parsed")
        try:
            if (not isinstance(parsed, dict) or parsed.get("protocol") != "maintenance"
                    or parsed.get("address") != self.address or parsed.get("op") != pending["op"]):
                raise ValueError(tr("维护响应与本次请求不匹配"))
            result = parsed.get("result")
            if result not in pending["allow"]:
                detail = tr(RESULT_NAMES[result]) if result in RESULT_NAMES else tr("未知分机结果码：{code}", code=result)
                raise ValueError(detail)
            pending["callback"](parsed)
        except (ValueError, KeyError, TypeError, RuntimeError) as exc:
            self._operation_error(tr(str(exc)))

    def _request_failed(self, profile_id: str, context: object, message: str) -> None:
        if self._pending is not None and context == self._pending["context"]:
            self._failure(profile_id, message)

    def _failure(self, profile_id: str, message: str) -> None:
        if profile_id != self.profile_id or self._pending is None:
            return
        self._pending = None
        if self._cancel_requested:
            self._read_cancelled()
        else:
            self._operation_error(tr("通信失败：{error}", error=message))

    def _operation_error(self, message: str) -> None:
        if self._writing and not self._recovering:
            self._recover_write(message)
        else:
            self._error(message)

    def _connection_changed(self, profile_id: str, connected: bool, message: str) -> None:
        if profile_id != self.profile_id or connected:
            return
        self._pending = None
        self._uncertain = self._uncertain or self._writing
        self.info = None
        self._paged = None
        self._show_probes([])
        self.info_label.setText(tr("串口已断开"))
        if self._cancel_requested:
            self._read_cancelled()
        elif self._busy:
            self._error(tr("连接已断开。请重连后重新读取分机。\n") + message)
        else:
            self._finish(tr("连接已断开，本地编辑保留。"), error=True)

    def _get_info(self, callback: Callable[[], None]) -> None:
        def done(parsed: dict) -> None:
            value = parsed["info"]
            if value.get("version") != 1:
                raise ValueError(tr("分机维护接口版本不受支持"))
            for name in ("grain_capacity", "ambient_capacity", "bus_count"):
                _integer(value.get(name), name, 1, MAX_BINDINGS)
            total = value["grain_capacity"] + value["ambient_capacity"]
            if total > MAX_BINDINGS or value["bus_count"] > 256:
                raise ValueError(tr("分机返回的容量超出登记工具支持范围"))
            _integer(value.get("active_count"), tr("已保存绑定数"), 0, total)
            _integer(value.get("discovered_count"), tr("已发现数"), 0, total)
            _integer(value.get("generation"), tr("配置代次"), 0, 0xFFFFFFFF)
            self.info = dict(value)
            self.scan_bus.setMaximum(value["bus_count"] - 1)
            self._point_range()
            self.info_label.setText(
                tr("维护接口 V{version} · 配置代次 {generation} · {bus} 支路 · 粮温容量 {grain} · "
                   "仓温仓湿容量 {ambient} · 扫描状态 {scan} · 分机暂存{staged}",
                   version=value["version"], generation=value["generation"], bus=value["bus_count"],
                   grain=value["grain_capacity"], ambient=value["ambient_capacity"], scan=value["scan_status"],
                   staged=tr("有未保存修改" if value["dirty"] else "无修改")))
            self._update_controls()
            callback()
        self._request(0, b"", done)

    def refresh(self) -> None:
        if self._busy or self._closed:
            return
        if self.dirty and QMessageBox.question(
                self, tr("重新读取分机"), tr("读取成功后将以分机已保存登记替换本地修改。继续？"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self._start(tr("正在读取分机信息…"))

        def loaded(rows: list[dict]) -> None:
            self._active = rows
            self._draft = rows.copy()
            self._base_generation = self.info["generation"]
            self._uncertain = False
            self._show_bindings()
            self._finish("")
            self.bindings_loaded.emit({"generation": self._base_generation, "bindings": [row.copy() for row in rows]})
        self._get_info(lambda: self._read_table(0, loaded))

    def _read_table(self, table: int, callback: Callable[[list[dict]], None]) -> None:
        info = self.info
        assert info is not None
        label = tr("已保存登记") if table == 0 else tr("分机暂存登记")

        def complete(rows: list[dict]) -> None:
            checked = normalise_bindings(rows, info)
            if table == 0 and len(checked) != info["active_count"]:
                raise ValueError(tr("已保存登记数量与分机信息不一致，请重新读取"))
            callback(checked)

        def legacy() -> None:
            if info["active_count"] == 0 and not info["dirty"]:
                complete([])
                return
            keys = [(kind, point) for kind in ("grain", "ambient")
                    for point in range(1, info[f"{kind}_capacity"] + 1)]
            rows = []

            def next_point(index: int) -> None:
                if index == len(keys):
                    if table == 0:
                        def verified() -> None:
                            if (self.info["generation"], self.info["active_count"]) != (info["generation"], info["active_count"]):
                                raise ValueError(tr("读取期间分机保存代次或数量已变化，请重新读取"))
                            complete(rows)
                        self._get_info(verified)
                    else:
                        complete(rows)
                    return
                kind, point = keys[index]
                self._status(tr("旧固件逐点读取{table} {current}/{total}…", table=label, current=index + 1, total=len(keys)), index, len(keys))

                def got(parsed: dict) -> None:
                    if parsed["result"] == 0:
                        binding = parsed["binding"]
                        if (self._key(binding) != (kind, point) or binding["table"] != table):
                            raise ValueError(tr("收到其他测点的绑定，已停止读取"))
                        rows.append(binding)
                    next_point(index + 1)
                payload = self._point_payload(kind, point) + bytes((table,))
                self._request(7, payload, got, allow=(0, 7), expected={"kind": kind, "point": point, "table": table})
            next_point(0)

        if self._paged is False:
            legacy()
            return
        rows = []
        snapshot: tuple[int, int] | None = None
        capacity = info["grain_capacity"] + info["ambient_capacity"]

        def page(offset: int) -> None:
            self._status(tr("正在读取{table} · 已收到 {count} 条…", table=label, count=offset), offset, snapshot[0] if snapshot else 0)
            fallback = offset == 0 and self._paged is None

            def got(parsed: dict) -> None:
                nonlocal snapshot
                if fallback and parsed["result"] in (1, 2):
                    self._paged = False
                    legacy()
                    return
                total = _integer(parsed["total"], tr("绑定数"), 0, capacity)
                revision = _integer(parsed["revision"], tr("绑定表修订号"), 0, 0xFFFFFFFF)
                records = parsed["records"]
                if (parsed["table"] != table or parsed["offset"] != offset or
                        not isinstance(records, list) or len(records) != min(BINDING_PAGE_LIMIT, total - offset)):
                    raise ValueError(tr("登记分页内容不一致，请重新读取"))
                if table == 0 and revision != info["generation"]:
                    raise ValueError(tr("读取期间分机保存代次已变化，请重新读取"))
                if snapshot is not None and snapshot != (total, revision):
                    raise ValueError(tr("读取期间登记表已变化，请重新读取"))
                snapshot = (total, revision)
                self._paged = True
                rows.extend(records)
                if len(rows) == total:
                    complete(rows)
                else:
                    page(len(rows))
            payload = bytes((table,)) + offset.to_bytes(2, "big") + bytes((BINDING_PAGE_LIMIT,))
            self._request(9, payload, got, allow=(0, 1, 2) if fallback else (0,),
                          expected={"table": table, "offset": offset, "limit": BINDING_PAGE_LIMIT,
                                    "bus_count": info["bus_count"]})
        page(0)

    def start_scan(self) -> None:
        if self._busy or not self.info:
            return
        self._start(tr("正在扫描探头…"))
        self._show_probes([])
        self._scan_started = time.monotonic()
        self._request(1, bytes((self.scan_bus.value(),)), self._scan_accepted)

    def _scan_accepted(self, _: dict) -> None:
        self._scan_timer.start()
        self._poll_scan()

    def _poll_scan(self) -> None:
        if self._pending is not None:
            return
        if time.monotonic() - self._scan_started > 60:
            self._error(tr("等待扫描结束超时，未使用部分结果。请重新读取分机。"))
            return

        def checked() -> None:
            if self.info["scan_busy"]:
                self._status(tr("正在扫描 · 已发现 {count} 个探头…", count=self.info['discovered_count']))
                return
            self._scan_timer.stop()
            status = self.info["scan_status"]
            if status != 0:
                raise ValueError(tr("扫描未完整成功：{error}。", error=tr(RESULT_NAMES.get(status, '未知错误'))))
            self._scan_pages(self.info["discovered_count"])
        self._get_info(checked)

    def _scan_pages(self, total: int) -> None:
        rows, roms = [], set()

        def page(offset: int) -> None:
            if offset == total:
                self._show_probes(rows)
                self._finish(tr("找到 {count} 个探头。", count=total) if total
                             else tr("未找到探头。请检查接线后重新扫描。"))
                return

            def got(parsed: dict) -> None:
                records = parsed["records"]
                if parsed["total"] != total or parsed["offset"] != offset or not records:
                    raise ValueError(tr("扫描分页不完整或前后不一致"))
                for record in records:
                    bus = _integer(record["bus"], tr("扫描支路"), 0, self.info["bus_count"] - 1)
                    rom = rom_bytes(record["rom"])
                    if rom in roms or bus != self.scan_bus.value():
                        raise ValueError(tr("扫描结果出现重复探头编码或支路不符"))
                    roms.add(rom)
                    rows.append({"bus": bus, "rom": rom})
                page(len(rows))
            self._request(2, offset.to_bytes(2, "big") + b"\x1b", got,
                          expected={"offset": offset, "scan_count": 27})
        page(0)

    def _edit(self, rows: list[dict], message: str) -> None:
        try:
            self._draft = normalise_bindings(rows, self.info)
        except ValueError as exc:
            self._error(str(exc))
            return
        self._show_bindings()
        if self.retry_button.isHidden():
            self._status(message)

    def bind_probe(self) -> None:
        probe = self.probe_combo.currentData()
        if self._busy or not self.info or self._active is None or probe is None:
            return
        key = self.kind_combo.currentData(), self.point_spin.value()
        binding = {"kind": key[0], "point": key[1], **probe}
        rows = [row for row in self._draft if self._key(row) != key]
        self._edit(rows + [binding], "")

    def unbind_probe(self) -> None:
        row = self.binding_table.currentRow()
        if (self._busy or self._active is None or not self.binding_table.selectedItems()
                or not 0 <= row < len(self._table_keys)):
            return
        key = self._table_keys[row]
        rows = [binding for binding in self._draft if self._key(binding) != key]
        if len(rows) == len(self._draft):
            original = next(binding for binding in self._active if self._key(binding) == key)
            self._edit(rows + [original], "")
        else:
            self._edit(rows, "")

    def reload(self) -> None:
        if self._busy or not self.dirty:
            return
        if QMessageBox.question(self, tr("撤销未保存修改"), tr("将本地登记恢复为上次读取的已保存登记？"),
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self._draft = self._active.copy()
        self._show_bindings()
        if self.retry_button.isHidden():
            self._status("")

    @staticmethod
    def _point_payload(kind: str, point: int) -> bytes:
        return bytes((1 if kind == "grain" else 2,)) + point.to_bytes(2, "big")

    def save(self) -> None:
        if self._busy or not self.info or not self.dirty or self._uncertain:
            return
        self._start(tr("正在核对分机当前登记…"))
        self._goal = self._draft.copy()

        def info_ready() -> None:
            if self.info["scan_busy"]:
                raise ValueError(tr("分机还在扫描，请等待后重试。"))
            normalise_bindings(self._goal, self.info)
            if self.info["generation"] != self._base_generation:
                raise ValueError(tr("分机已保存的登记有变化，请重新读取并核对后再保存。"))

            def checked(rows: list[dict]) -> None:
                if rows != self._active:
                    raise ValueError(tr("分机已保存的登记有变化，请重新读取并核对后再保存。"))
                if self.info["dirty"]:
                    self._read_table(1, staged_ready)
                else:
                    staged_ready(rows)

            def staged_ready(rows: list[dict]) -> None:
                board_dirty = self.info["dirty"]
                if rows != self._active and rows != self._goal and QMessageBox.question(
                        self, tr("核对分机暂存修改"), tr("分机上还有一份未保存的登记。用本地登记替换并保存？"),
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                    self._finish(tr("已取消保存，本地编辑保留。"))
                    return

                def still_current() -> None:
                    if (self.info["scan_busy"] or self.info["generation"] != self._base_generation
                            or self.info["active_count"] != len(self._active) or self.info["dirty"] != board_dirty):
                        raise ValueError(tr("分机状态已变化，请重新读取并核对后再保存。"))
                    self._write_changes(rows)
                self._get_info(still_current)
            self._read_table(0, checked)
        self._get_info(info_ready)

    def _write_changes(self, staged: list[dict]) -> None:
        assert self._goal is not None
        old = {self._key(row): row for row in staged}
        target = {self._key(row): row for row in self._goal}
        target_by_rom = {row["rom"]: key for key, row in target.items()}
        # Free the old slot before binding a ROM to a different slot, including swaps.
        operations = [(4, self._point_payload(*key)) for key, row in old.items()
                      if key not in target or target_by_rom.get(row["rom"], key) != key]
        operations.extend((3, self._point_payload(*key) + bytes((row["bus"],)) + row["rom"])
                          for key, row in target.items() if old.get(key) != row)

        def step(index: int) -> None:
            if index < len(operations):
                self._status(tr("正在更新分机登记 {current}/{total}…", current=index + 1, total=len(operations)), index, len(operations))
                op, payload = operations[index]
                self._request(op, payload, lambda _: step(index + 1))
                return
            self._status(tr("正在核对分机暂存登记…"))

            def checked(rows: list[dict]) -> None:
                if rows != self._goal:
                    raise ValueError(tr("分机暂存登记与本地登记不一致，未提交保存。"))
                self._status(tr("正在保存到分机…"))
                self._save_sent = True
                self._request(5, b"", lambda _: self._get_info(self._verify_saved))

            def info_ready() -> None:
                if self.info["generation"] != self._base_generation:
                    raise ValueError(tr("保存前分机配置代次变化，未提交保存。"))
                self._read_table(1, checked)
            self._get_info(info_ready)
        step(0)

    def _verify_saved(self) -> None:
        if not self.info["config_valid"] or self.info["dirty"]:
            raise ValueError(tr("分机尚未确认保存，请回读核对。"))

        def checked(rows: list[dict]) -> None:
            if rows != self._goal:
                raise ValueError(tr("回读的已保存登记与本地登记不一致。"))
            self._saved(rows, tr("已保存并核对 {count} 条登记。", count=len(rows)))
        self._read_table(0, checked)

    def _saved(self, rows: list[dict], message: str) -> None:
        self._active = rows
        self._draft = rows.copy()
        self._base_generation = self.info["generation"]
        self._uncertain = False
        self._show_bindings()
        self._finish(message)
        self.bindings_saved.emit({"generation": self._base_generation, "bindings": [row.copy() for row in rows]})

    def _recover_write(self, cause: str) -> None:
        self._recovering = True
        self._status(tr("写入未获确认，正在只读回查分机…"))

        def checked(rows: list[dict]) -> None:
            self._uncertain = False
            if self._save_sent and rows == self._goal and self.info["config_valid"] and not self.info["dirty"]:
                self._saved(rows, tr("保存响应未确认；只读回查已与目标一致，分机已保存。"))
                return
            location = tr("已保存登记") if self._save_sent else tr("暂存登记")
            self._recovering = False
            self._error(tr("{error}\n已回读分机{table}（{count} 条），未自动重发。请核对后再保存。", error=cause, table=location, count=len(rows)))
        self._get_info(lambda: self._read_table(0 if self._save_sent else 1, checked))

    def backup(self) -> None:
        if self._busy or not self.info:
            return
        self._start(tr("正在读取分机登记以备份…"))

        def info_ready() -> None:
            if not self.info["config_valid"]:
                raise ValueError(tr("分机尚无已保存登记，请先保存。"))
            generation = self.info["generation"]

            def rows_ready(rows: list[dict]) -> None:
                def checked() -> None:
                    if self.info["generation"] != generation:
                        raise ValueError(tr("备份读取期间配置已变化，请重新备份。"))
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
                    filename, _ = QFileDialog.getSaveFileName(
                        self, tr("备份已保存登记"), tr("分机{address}_登记备份.json", address=self.address), "JSON (*.json)")
                    if not filename:
                        self._finish(tr("已取消备份。"))
                        return
                    try:
                        destination = Path(filename)
                        temporary = destination.with_name(destination.name + ".tmp")
                        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
                        temporary.replace(destination)
                    except OSError as exc:
                        self._error(tr("备份文件未写入成功：{error}", error=exc))
                        return
                    self._finish(tr("已备份 {count} 条登记：{path}", count=len(rows), path=filename))
                self._get_info(checked)
            self._read_table(0, rows_ready)
        self._get_info(info_ready)

    def restore(self) -> None:
        if self._busy or not self.info or self._active is None:
            return
        filename, _ = QFileDialog.getOpenFileName(self, tr("选择登记备份"), "", "JSON (*.json)")
        if not filename:
            return
        try:
            path = Path(filename)
            if path.stat().st_size > 2_000_000:
                raise ValueError(tr("备份文件过大"))
            document = json.loads(path.read_text(encoding="utf-8"))
            target = validate_backup(document, self.address, self.info)
            if document["simulated"] != self._simulator:
                raise ValueError(tr("模拟设备与真实设备的备份不能混用"))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self._error(tr("备份未导入：{error}", error=tr(str(exc))))
            return
        source = document["board"].get("name", tr("未命名"))
        if QMessageBox.question(
                self, tr("导入到本地登记表"),
                tr("从“{name}”导入 {count} 条登记，替换当前待编辑表？\n"
                   "请核对探头位置，点击“保存到分机”才会提交。", name=source, count=len(target)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._edit(target, tr("已导入 {count} 条到本地登记表。", count=len(target)))

    def reject(self) -> None:
        if self._writing:
            QMessageBox.information(self, tr("正在保存"), tr("请等待分机保存和核对完成后再关闭。"))
            return
        if self._busy:
            self._close_after_read = True
            self.cancel_read()
            return
        if self.dirty or self._uncertain:
            message = (tr("分机保存状态尚未核对，关闭后请重新读取核对。\n") if self._uncertain else "")
            if QMessageBox.question(self, tr("关闭登记"), message + tr("关闭后将丢弃本地未保存修改。继续？"),
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        super().reject()

    def closeEvent(self, event: Any) -> None:
        event.ignore()
        self.reject()

    def _disconnect(self, *_: Any) -> None:
        if self._closed:
            return
        self._closed = True
        self._scan_timer.stop()
        connections = [(self.manager.response, self._response), (self._failure_signal, self._failure_handler)]
        if hasattr(self.manager, "connection_changed"):
            connections.append((self.manager.connection_changed, self._connection_changed))
        for signal, slot in connections:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

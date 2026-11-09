from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .registration import RegistrationDialog
from .domain import source_for_port


def button(text, callback, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(callback)
    return widget


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setAlternatingRowColors(True)
    widget.verticalHeader().hide()
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    widget.verticalHeader().setDefaultSectionSize(28)
    return widget


def fill_table(widget, rows):
    widget.setSortingEnabled(False)
    widget.setRowCount(len(rows))
    for row, values in enumerate(rows):
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value) if value is not None else "—")
            item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            widget.setItem(row, column, item)


class PortDialog(QDialog):
    def __init__(self, manager, current=None, parent=None, demo=False):
        super().__init__(parent)
        current = current or {}
        self.setWindowTitle("串口连接设置")
        self.setMinimumWidth(430)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(current.get("name", ""))
        self.port = QComboBox()
        self.port.setEditable(True)
        if demo:
            self.port.addItem("SIMULATOR")
        else:
            self.port.addItems([p["port_name"] for p in manager.available_ports()])
        self.port.setCurrentText(current.get("port_name", self.port.itemText(0)))
        self.baud = QComboBox()
        self.baud.addItems([str(n) for n in (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)])
        self.baud.setCurrentText(str(current.get("baud", 9600)))
        self.parity = QComboBox()
        for text, value in (("无校验 N", "N"), ("偶校验 E", "E"), ("奇校验 O", "O")):
            self.parity.addItem(text, value)
        self.parity.setCurrentIndex(self.parity.findData(current.get("parity", "N")))
        self.stop = QComboBox()
        self.stop.addItems(["1", "2"])
        self.stop.setCurrentText(str(current.get("stop_bits", 1)))
        for label, widget in (("连接名称", self.name), ("串口", self.port), ("波特率", self.baud),
                              ("校验位", self.parity), ("停止位", self.stop)):
            form.addRow(label, widget)
        layout.addLayout(form)
        hint = QLabel("多个模拟通道可填写 simulator://2 等名称。" if demo else "选择系统识别的串口；模拟设备请通过单独的演示入口使用。")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        layout.addWidget(hint)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        actions.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        layout.addWidget(actions)

    def values(self):
        return dict(name=self.name.text().strip(), port_name=self.port.currentText().strip(),
                    baud=int(self.baud.currentText()), parity=self.parity.currentData(), stop_bits=int(self.stop.currentText()))


class DeviceDialog(QDialog):
    def __init__(self, metadata, current=None, parent=None):
        super().__init__(parent)
        current = current or {}
        self.setWindowTitle("分机设置")
        self.setMinimumWidth(430)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(current.get("name", ""))
        self.warehouse, self.port, self.protocol = QComboBox(), QComboBox(), QComboBox()
        for row in metadata["warehouses"]:
            self.warehouse.addItem(row["name"], row["id"])
        for row in metadata["ports"]:
            self.port.addItem(f"{row['name']} · {row['port_name']}", row["id"])
        self.protocol.addItems(["LC-M3", "Modbus RTU"])
        self.address, self.grain, self.ambient = QSpinBox(), QSpinBox(), QSpinBox()
        self.address.setRange(1, 247)
        self.grain.setRange(0, 165)
        self.ambient.setRange(0, 5)
        self.address.setValue(current.get("address", 1))
        self.grain.setValue(current.get("grain_count", 165))
        self.ambient.setValue(current.get("ambient_count", 1))
        self.protocol.setCurrentText(current.get("protocol", "LC-M3"))
        for combo, key in ((self.warehouse, "warehouse_id"), (self.port, "port_id")):
            if key in current:
                combo.setCurrentIndex(combo.findData(current[key]))
        for label, widget in (("分机名称", self.name), ("所属仓房", self.warehouse), ("连接", self.port),
                              ("分机地址", self.address), ("采集协议", self.protocol), ("粮温查询点数", self.grain),
                              ("仓温湿查询点数", self.ambient)):
            form.addRow(label, widget)
        layout.addLayout(form)
        hint = QLabel("查询点数用于轮询范围；探头 ROM 与物理位置在登记和布点中单独设置。当前协议针对本次 GXHT3W 固件。")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        layout.addWidget(hint)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        actions.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        layout.addWidget(actions)

    def values(self):
        return dict(name=self.name.text().strip(), warehouse_id=self.warehouse.currentData(), port_id=self.port.currentData(),
                    address=self.address.value(), protocol=self.protocol.currentText(), grain_count=self.grain.value(),
                    ambient_count=self.ambient.value())


class DevicesPage(QWidget):
    def __init__(self, repository, controller, parent=None):
        super().__init__(parent)
        self.repo, self.controller = repository, controller
        self._metadata = controller.metadata
        self._registration_wait = None
        self._registration_active = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("串口连接"))
        row.addStretch()
        row.addWidget(button("新增连接", self.add_port, True))
        row.addWidget(button("编辑", self.edit_port))
        row.addWidget(button("连接 / 断开", self.toggle_port))
        row.addWidget(button("删除连接", self.delete_port))
        layout.addLayout(row)
        self.ports_table = table(["连接名称", "串口", "串口参数", "来源", "连接状态"])
        self.ports_table.setMaximumHeight(150)
        layout.addWidget(self.ports_table)
        row = QHBoxLayout()
        row.addWidget(QLabel("仓房与分机"))
        row.addStretch()
        row.addWidget(button("新增仓房", self.add_warehouse))
        row.addWidget(button("新增分机", self.add_device, True))
        row.addWidget(button("编辑分机", self.edit_device))
        row.addWidget(button("在线查询", self.ping))
        row.addWidget(button("探头登记", self.register, True))
        row.addWidget(button("删除分机", self.delete_device))
        layout.addLayout(row)
        self.devices_table = table(["分机", "仓房", "连接", "地址", "协议", "粮温 / 仓温湿", "来源"])
        layout.addWidget(self.devices_table, 1)
        hint = QLabel("先添加仓房和串口连接，再添加分机。每个连接的参数独立；探头登记需先连接对应串口。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        controller.metadata_changed.connect(self.refresh)
        controller.connections_changed.connect(self.refresh_status)

    def refresh(self, metadata):
        self._metadata = metadata
        fill_table(self.devices_table, [[d["name"], d["warehouse_name"], d["connection_name"], d["address"],
                                        d["protocol"], f"{d['grain_count']} / {d['ambient_count']}",
                                        "模拟" if d["source"] == "simulator" else "实机"] for d in metadata["devices"]])
        self.refresh_status()

    def refresh_status(self):
        selected = self.ports_table.currentRow()
        fill_table(self.ports_table, [[p["name"], p["port_name"], f"{p['baud']} / 8{p['parity']}{p['stop_bits']}",
                                     "模拟" if p["source"] == "simulator" else "实机",
                                     "已连接" if self.controller.manager.is_connected(str(p["id"])) else "未连接"]
                                    for p in self._metadata["ports"]])
        if 0 <= selected < self.ports_table.rowCount():
            self.ports_table.selectRow(selected)

    def selected(self, category):
        widget = self.ports_table if category == "ports" else self.devices_table
        index = widget.currentRow()
        rows = self._metadata[category]
        if not 0 <= index < len(rows):
            QMessageBox.information(self, "请选择", "请先选择一行记录。")
            return None
        return rows[index]

    def save(self, method, **kwargs):
        if method == "save_port":
            expected = "simulator" if self.controller.demo else "serial"
            if source_for_port(kwargs["port_name"]) != expected:
                QMessageBox.information(self, "数据模式不同", "模拟与实机连接使用独立入口，不能加入当前数据库。")
                return
        self.repo.call(method, callback=lambda _: self.controller.reload_metadata(), **kwargs)

    def add_warehouse(self):
        name, ok = QInputDialog.getText(self, "新增仓房", "仓房名称")
        if ok and name.strip():
            self.save("save_warehouse", name=name)

    def add_port(self):
        dialog = PortDialog(self.controller.manager, parent=self, demo=self.controller.demo)
        if dialog.exec():
            self.save("save_port", **dialog.values())

    def edit_port(self):
        current = self.selected("ports")
        if not current:
            return
        if self.controller.manager.is_connected(str(current["id"])):
            QMessageBox.information(self, "连接仍打开", "请先断开此连接，再修改串口设置。")
            return
        dialog = PortDialog(self.controller.manager, current, self, demo=self.controller.demo)
        if dialog.exec():
            self.save("save_port", id=current["id"], **dialog.values())

    def toggle_port(self):
        port = self.selected("ports")
        if not port:
            return
        profile = str(port["id"])
        if self.controller.manager.is_connected(profile):
            self.controller.manager.disconnect_port(profile)
        else:
            self.controller.connect_port(port)

    def delete_port(self):
        port = self.selected("ports")
        if port and QMessageBox.question(self, "删除连接", f"删除连接“{port['name']}”？需先处理其关联分机。") == QMessageBox.StandardButton.Yes:
            self.controller.manager.disconnect_port(str(port["id"]))
            self.save("delete_port", id=port["id"])

    def add_device(self):
        if not self._metadata["warehouses"] or not self._metadata["ports"]:
            QMessageBox.information(self, "尚未配置", "请先新增仓房和串口连接。")
            return
        dialog = DeviceDialog(self._metadata, parent=self)
        if dialog.exec():
            self.save("save_device", **dialog.values())

    def edit_device(self):
        device = self.selected("devices")
        if not device:
            return
        if self.controller.manager.is_connected(str(device["port_id"])):
            QMessageBox.information(self, "连接仍打开", "请先断开对应串口，再修改分机配置。")
            return
        dialog = DeviceDialog(self._metadata, device, self)
        if dialog.exec():
            self.save("save_device", id=device["id"], **dialog.values())

    def delete_device(self):
        device = self.selected("devices")
        if device and not self.controller.manager.is_connected(str(device["port_id"])):
            if QMessageBox.question(self, "删除分机", f"删除“{device['name']}”？已有历史或报警的分机会保留，不执行删除。") == QMessageBox.StandardButton.Yes:
                self.save("delete_device", id=device["id"])
        elif device:
            QMessageBox.information(self, "连接仍打开", "请先断开对应串口。")

    def ping(self):
        device = self.selected("devices")
        if device:
            self.controller.query_online(device)

    def register(self):
        if self._registration_active:
            self.controller.log.emit("正在等待或执行探头登记，请完成当前操作。")
            return
        device = self.selected("devices")
        if not device:
            return
        profile = str(device["port_id"])
        if not self.controller.manager.is_connected(profile):
            QMessageBox.information(self, "未连接", "请先连接对应串口。")
            return
        self.controller.pause_port(profile)
        self._registration_active = True
        started = time.monotonic()
        timer = QTimer(self)
        self._registration_wait = timer
        timer.setInterval(100)
        self.controller.log.emit(f"{device['name']}：等待本口已有采集完成，随后打开登记窗口")

        def ready():
            if not self.controller.manager.is_connected(profile):
                timer.stop()
                timer.deleteLater()
                self._registration_wait = None
                self._registration_active = False
                self.controller.resume_port(profile)
                self.controller.log.emit("串口已断开，取消等待登记。")
                return
            if self.controller.manager.busy(profile):
                if time.monotonic() - started > 60:
                    timer.stop()
                    timer.deleteLater()
                    self._registration_wait = None
                    self._registration_active = False
                    self.controller.resume_port(profile)
                    QMessageBox.information(self, "串口仍忙", "现有请求尚未完成，请稍后再打开登记。")
                return
            timer.stop()
            timer.deleteLater()
            self._registration_wait = None
            try:
                dialog = RegistrationDialog(self.controller.manager, profile, device["address"], device["name"], self)
                dialog.bindings_saved.connect(lambda payload: self._bindings_saved(device, payload))
                dialog.exec()
            finally:
                self._registration_active = False
                self.controller.resume_port(profile)
        timer.timeout.connect(ready)
        timer.start()

    def _bindings_saved(self, device, payload):
        bindings = payload["bindings"]
        grain = max([device["grain_count"]] + [r["point"] for r in bindings if r["kind"] == "grain"])
        ambient = max([device["ambient_count"]] + [r["point"] for r in bindings if r["kind"] == "ambient"])

        def saved(_):
            self.repo.call("set_bindings", device["id"], bindings, callback=lambda _: self.controller.reload_metadata())
            self.controller.log.emit(f"{device['name']}：活动绑定已回读，更新电脑档案（配置代次 {payload['generation']}）")
        self.repo.call("save_device", device["name"], device["warehouse_id"], device["port_id"],
                       device["address"], device["protocol"], grain, ambient, id=device["id"], callback=saved)

from __future__ import annotations

import time

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .domain import source_for_port
from .filter_panel import FilterPanel
from .i18n import tr
from .registration import RegistrationDialog


def button(text, callback, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(callback)
    return widget


def _toolbar():
    widget = QWidget()
    widget.setObjectName("toolbar")
    row = QHBoxLayout(widget)
    row.setContentsMargins(0, 0, 0, 0)
    return widget, row


def _more_button(actions):
    widget = QPushButton(tr("更多"))
    widget.setObjectName("secondary")
    menu = QMenu(widget)
    for title, callback in actions:
        if title:
            menu.addAction(title, callback)
        else:
            menu.addSeparator()
    widget.setMenu(menu)
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
        self.setWindowTitle(tr("串口连接设置"))
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
        self.baud.setCurrentText(str(current.get("baud", 1200)))
        self.parity = QComboBox()
        for text, value in ((tr("无校验 N"), "N"), (tr("偶校验 E"), "E"), (tr("奇校验 O"), "O")):
            self.parity.addItem(text, value)
        self.parity.setCurrentIndex(self.parity.findData(current.get("parity", "N")))
        self.stop = QComboBox()
        self.stop.addItems(["1", "2"])
        self.stop.setCurrentText(str(current.get("stop_bits", 1)))
        port_row = QHBoxLayout()
        port_row.addWidget(self.port, 1)
        def refresh_ports():
            selected = self.port.currentText()
            self.port.clear()
            self.port.addItems(["SIMULATOR"] if demo else [p["port_name"] for p in manager.available_ports()])
            if selected:
                self.port.setCurrentText(selected)
        port_row.addWidget(button(tr("刷新"), refresh_ports))
        form.addRow(tr("串口"), port_row)
        for label, widget in ((tr("连接名称"), self.name), (tr("波特率"), self.baud),
                              (tr("校验位"), self.parity), (tr("停止位"), self.stop)):
            form.addRow(label, widget)
        layout.addLayout(form)
        self.port.setToolTip(tr("多个模拟通道可填写 simulator://2 等名称。") if demo else tr("选择系统识别的串口。"))
        self.baud.setToolTip(tr("当前 LC-M3 固件使用 1200 / 8N1。"))
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        actions.button(QDialogButtonBox.StandardButton.Save).setText(tr("保存"))
        actions.button(QDialogButtonBox.StandardButton.Save).setObjectName("primary")
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消"))
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
        self.setWindowTitle(tr("分机设置"))
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
        for label, widget in ((tr("分机名称"), self.name), (tr("所属仓房"), self.warehouse), (tr("串口"), self.port),
                              (tr("板上地址"), self.address)):
            form.addRow(label, widget)
        layout.addLayout(form)
        advanced_toggle = QPushButton(tr("高级选项"))
        advanced_toggle.setObjectName("quietButton")
        advanced_toggle.setCheckable(True)
        advanced_toggle.setToolTip(tr("采集协议及旧机查询范围。已登记的分机按实际测点采集。"))
        advanced = QWidget()
        advanced_form = QFormLayout(advanced)
        for label, widget in ((tr("采集协议"), self.protocol), (tr("旧机粮温最大点号"), self.grain), (tr("旧机仓温湿最大点号"), self.ambient)):
            advanced_form.addRow(label, widget)
        advanced_toggle.toggled.connect(advanced.setVisible)
        advanced.hide()
        layout.addWidget(advanced_toggle)
        layout.addWidget(advanced)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        actions.button(QDialogButtonBox.StandardButton.Save).setText(tr("保存"))
        actions.button(QDialogButtonBox.StandardButton.Save).setObjectName("primary")
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消"))
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        layout.addWidget(actions)

    def values(self):
        return dict(name=self.name.text().strip(), warehouse_id=self.warehouse.currentData(), port_id=self.port.currentData(),
                    address=self.address.value(), protocol=self.protocol.currentText(), grain_count=self.grain.value(),
                    ambient_count=self.ambient.value())


class DevicesPage(QWidget):
    mode_requested = Signal(str)

    def __init__(self, repository, controller, parent=None):
        super().__init__(parent)
        self.repo, self.controller = repository, controller
        self._metadata = controller.metadata
        self._registration_wait = None
        self._registration_active = False
        self.registration_dialog = None
        self.mode = "devices"
        self._saving = False
        self._sync_pending = {}
        self._sync_failed = set()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages)
        device_page, port_page, registration_page = QWidget(), QWidget(), QWidget()
        for page in (device_page, port_page, registration_page):
            self.pages.addWidget(page)
        devices = QVBoxLayout(device_page)
        toolbar, row = _toolbar()
        row.addWidget(button(tr("新增分机"), self.add_device, True))
        row.addStretch()
        self.register_selected_button = button(tr("登记探头"), self.register)
        self.register_selected_button.setObjectName("secondary")
        row.addWidget(self.register_selected_button)
        self.device_more = _more_button(((tr("编辑分机"), self.edit_device), (tr("查询在线状态"), self.ping),
                                         (None, None), (tr("删除分机…"), self.delete_device)))
        row.addWidget(self.device_more)
        devices.addWidget(toolbar)
        self.devices_table = table([tr("分机 / 仓房"), tr("实际串口"), tr("板上地址"),
                                    tr("采集协议"), tr("登记 / 查询范围"), tr("通信状态")])
        devices.addWidget(self.devices_table, 1)
        ports = QVBoxLayout(port_page)
        toolbar, row = _toolbar()
        row.addWidget(button(tr("新增串口"), self.add_port, True))
        row.addStretch()
        self.toggle_selected_port_button = button(tr("打开串口"), self.toggle_port)
        self.toggle_selected_port_button.setObjectName("secondary")
        row.addWidget(self.toggle_selected_port_button)
        self.port_more = _more_button(((tr("编辑参数"), self.edit_port), (None, None), (tr("删除串口…"), self.delete_port)))
        row.addWidget(self.port_more)
        ports.addWidget(toolbar)
        self.ports_table = table([tr("连接名称"), tr("实际端口"), tr("串口参数"), tr("关联分机"), tr("端口状态")])
        ports.addWidget(self.ports_table, 1)
        self.registration_layout = QVBoxLayout(registration_page)
        self.registration_layout.setContentsMargins(0, 0, 0, 0)
        self.registration_selector = FilterPanel()
        self.registration_device = QComboBox()
        self.registration_device.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.registration_device.setMinimumContentsLength(12)
        self.registration_selector.add_field(tr("分机"), self.registration_device)
        self.open_registration_button = button(tr("连接并读取"), self.register, True)
        self.registration_selector.add_action(self.open_registration_button)
        self.registration_layout.addWidget(self.registration_selector)
        self.registration_hint = QLabel()
        self.registration_hint.setWordWrap(True)
        self.registration_layout.addWidget(self.registration_hint)
        self.registration_hint.hide()
        self.registration_layout.addStretch(1)
        self.devices_table.itemSelectionChanged.connect(self._update_row_actions)
        self.ports_table.itemSelectionChanged.connect(self._update_row_actions)
        controller.metadata_changed.connect(self.refresh)
        controller.connections_changed.connect(self.refresh_status)
        self._update_row_actions()
        self.open_registration_button.setEnabled(False)

    def _update_row_actions(self):
        has_device = self._selected_id(self.devices_table) is not None
        self.register_selected_button.setEnabled(has_device)
        self.device_more.setEnabled(has_device)
        port_id = self._selected_id(self.ports_table)
        self.toggle_selected_port_button.setEnabled(port_id is not None)
        self.port_more.setEnabled(port_id is not None)
        connected = port_id is not None and self.controller.manager.is_connected(str(port_id))
        self.toggle_selected_port_button.setText(tr("关闭串口") if connected else tr("打开串口"))

    def _registration_feedback(self, text=""):
        self.registration_hint.setText(text)
        self.registration_hint.setVisible(bool(text))

    @staticmethod
    def _selected_id(widget):
        if not widget.selectedItems():
            return None
        item = widget.item(widget.currentRow(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    @staticmethod
    def _restore_selection(widget, rows, selected):
        widget.clearSelection()
        for index, row in enumerate(rows):
            widget.item(index, 0).setData(Qt.ItemDataRole.UserRole, row["id"])
            if row["id"] == selected:
                widget.selectRow(index)

    def refresh(self, metadata):
        selected = self._selected_id(self.devices_table)
        selected_registration = self.registration_device.currentData()
        self._metadata = metadata
        self._fill_devices(selected)
        self.registration_device.blockSignals(True)
        self.registration_device.clear()
        for d in metadata["devices"]:
            label = tr("{name} · {warehouse} · {port} · 地址 {address}", name=d["name"],
                       warehouse=d["warehouse_name"], port=d["port_name"], address=d["address"])
            self.registration_device.addItem(label, d["id"])
        self.registration_device.setCurrentIndex(max(0, self.registration_device.findData(selected_registration)))
        self.registration_device.blockSignals(False)
        self.open_registration_button.setEnabled(bool(metadata["devices"]) and not self._registration_active)
        if not self._registration_active:
            if not metadata["devices"]:
                self._registration_feedback(tr("请先新增分机。"))
            elif self.registration_hint.text() == tr("请先新增分机。"):
                self._registration_feedback()
        self.refresh_status()

    def _fill_devices(self, selected):
        rows = []
        for d in self._metadata["devices"]:
            points = d.get("registered_points")
            scope = (tr("已登记 {count} 点", count=len(points)) if points is not None
                     else tr("待登记") if d.get("registration_required")
                     else tr("旧机范围 {grain} / {ambient}", grain=d["grain_count"], ambient=d["ambient_count"]))
            rows.append([f"{d['name']} / {d['warehouse_name']}", d["port_name"], d["address"], d["protocol"], scope,
                         tr(self.controller.communication_state(d))])
        with QSignalBlocker(self.devices_table):
            fill_table(self.devices_table, rows)
            self._restore_selection(self.devices_table, self._metadata["devices"], selected)

    def refresh_status(self):
        selected = self._selected_id(self.ports_table)
        with QSignalBlocker(self.ports_table):
            fill_table(self.ports_table, [[p["name"], p["port_name"], f"{p['baud']} / 8{p['parity']}{p['stop_bits']}",
                                         "、".join(d["name"] for d in self._metadata["devices"] if d["port_id"] == p["id"]) or tr("未关联"),
                                         tr("已打开") if self.controller.manager.is_connected(str(p["id"])) else tr("已关闭")]
                                        for p in self._metadata["ports"]])
            self._restore_selection(self.ports_table, self._metadata["ports"], selected)
        self._fill_devices(self._selected_id(self.devices_table))
        self._update_row_actions()

    def can_leave(self):
        if self.registration_dialog is not None:
            self.registration_dialog.reject()
            return self.registration_dialog is None
        if self._registration_wait:
            self._registration_wait.stop()
            self._registration_wait.deleteLater()
            self._registration_wait = None
            self._finish_registration()
        return True

    def set_mode(self, mode):
        if mode != self.mode and not self.can_leave():
            return False
        self.mode = mode
        self.pages.setCurrentIndex({"devices": 0, "ports": 1, "registration": 2}[mode])
        return True

    def selected(self, category):
        widget = self.ports_table if category == "ports" else self.devices_table
        index = widget.currentRow()
        rows = self._metadata[category]
        if not widget.selectedItems() or not 0 <= index < len(rows):
            QMessageBox.information(self, tr("请选择"), tr("请先选择一行记录。"))
            return None
        return rows[index]

    def save(self, method, callback=None, **kwargs):
        if self._saving:
            return
        if method == "save_port":
            expected = "simulator" if self.controller.demo else "serial"
            if source_for_port(kwargs["port_name"]) != expected:
                QMessageBox.information(self, tr("数据模式不同"), tr("模拟与实机连接使用独立入口，不能加入当前数据库。"))
                return
        self._saving = self.controller.config_saving = True
        self.setEnabled(False)
        def failed(message):
            self._saving = self.controller.config_saving = False
            self.setEnabled(True)
            self.repo.error.emit(tr("设备配置保存失败：{error}", error=message))
        def done(value):
            def loaded(_):
                self._saving = self.controller.config_saving = False
                self.setEnabled(True)
                if callback:
                    callback(value)
            self.controller.reload_metadata(callback=loaded, on_error=failed)
        self.repo.call(method, callback=done, on_error=failed, **kwargs)

    def add_warehouse(self):
        name, ok = QInputDialog.getText(self, tr("新增仓房"), tr("仓房名称"))
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
            QMessageBox.information(self, tr("连接仍打开"), tr("请先断开此连接，再修改串口设置。"))
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
        if port and any(d["port_id"] == port["id"] for d in self._metadata["devices"]):
            QMessageBox.information(self, tr("串口有关联分机"), tr("请先修改或移除关联分机；本次不会断开串口。"))
            return
        if port and QMessageBox.question(
                self, tr("删除连接"),
                tr("删除连接“{name}”？需先处理其关联分机。", name=port["name"])) == QMessageBox.StandardButton.Yes:
            self.controller.manager.disconnect_port(str(port["id"]))
            self.save("delete_port", id=port["id"])

    def add_device(self):
        if not self._metadata["warehouses"]:
            name, ok = QInputDialog.getText(self, tr("新增分机 · 创建仓房"), tr("先填写所属仓房名称"))
            if ok and name.strip():
                self.save("save_warehouse", name=name.strip(), callback=lambda _: self.add_device())
            return
        if not self._metadata["ports"]:
            dialog = PortDialog(self.controller.manager, parent=self, demo=self.controller.demo)
            if dialog.exec():
                self.save("save_port", callback=lambda _: self.add_device(), **dialog.values())
            return
        dialog = DeviceDialog(self._metadata, parent=self)
        if dialog.exec():
            self.save("save_device", registration_required=True, **dialog.values())

    def edit_device(self):
        device = self.selected("devices")
        if not device:
            return
        if self.controller.manager.is_connected(str(device["port_id"])):
            QMessageBox.information(self, tr("连接仍打开"), tr("请先断开对应串口，再修改分机配置。"))
            return
        dialog = DeviceDialog(self._metadata, device, self)
        if dialog.exec():
            self.save("save_device", id=device["id"], **dialog.values())

    def delete_device(self):
        device = self.selected("devices")
        if device and not self.controller.manager.is_connected(str(device["port_id"])):
            if QMessageBox.question(
                    self, tr("删除分机"),
                    tr("删除“{name}”？已有历史或报警的分机会保留，不执行删除。",
                       name=device["name"])) == QMessageBox.StandardButton.Yes:
                self.save("delete_device", id=device["id"])
        elif device:
            QMessageBox.information(self, tr("连接仍打开"), tr("请先断开对应串口。"))

    def ping(self):
        device = self.selected("devices")
        if device:
            self.controller.query_online(device)

    def register(self):
        if self._registration_active or self._saving:
            return
        if self.mode == "registration":
            device = self.controller.device(self.registration_device.currentData())
        else:
            device = self.selected("devices")
        if not device:
            return
        self.set_mode("registration")
        self.mode_requested.emit("registration")
        self.registration_device.setCurrentIndex(self.registration_device.findData(device["id"]))
        self._registration_profile = profile = str(device["port_id"])
        self.controller.pause_port(profile)
        self._registration_active = True
        self.registration_device.setEnabled(False)
        self.open_registration_button.setEnabled(False)
        self._registration_feedback(tr("正在连接并读取…"))
        if not self.controller.manager.is_connected(profile):
            port = next(p for p in self._metadata["ports"] if p["id"] == device["port_id"])
            self.controller.connect_port(port)
        started = time.monotonic()
        timer = QTimer(self)
        self._registration_wait = timer
        timer.setInterval(100)

        def ready():
            if not self.controller.manager.is_connected(profile):
                self.can_leave()
                self._registration_feedback(tr("串口连接失败，请检查串口设置。"))
                return
            if self.controller.manager.busy(profile):
                if time.monotonic() - started > 60:
                    self.can_leave()
                    self._registration_feedback(tr("串口仍忙，请完成当前任务后重试。"))
                return
            timer.stop(); timer.deleteLater(); self._registration_wait = None
            dialog = RegistrationDialog(self.controller.manager, profile, device["address"], device["name"], self,
                                        embedded=True, port_name=device.get("port_name"))
            self.registration_dialog = dialog
            dialog.bindings_saved.connect(lambda payload: self._bindings_saved(device, payload))
            dialog.bindings_loaded.connect(lambda payload: self._bindings_saved(device, payload))
            dialog.finished.connect(self._finish_registration)
            self.registration_layout.setStretch(self.registration_layout.count() - 1, 0)
            self.registration_layout.insertWidget(2, dialog, 1)
            self._registration_feedback()
            self.registration_selector.hide()
            dialog.show()
        timer.timeout.connect(ready)
        timer.start()

    def _finish_registration(self, *_):
        dialog = self.registration_dialog
        self.registration_dialog = None
        if dialog is not None:
            self.registration_layout.removeWidget(dialog)
            dialog.hide(); dialog.deleteLater()
        self._registration_active = False
        self.registration_layout.setStretch(self.registration_layout.count() - 1, 1)
        self.registration_device.setEnabled(True)
        self.open_registration_button.setEnabled(bool(self._metadata["devices"]))
        self.registration_selector.show()
        self._registration_feedback()
        profile = getattr(self, "_registration_profile", "")
        if not self._sync_pending.get(profile) and profile not in self._sync_failed:
            self.controller.resume_port(profile)

    def _bindings_saved(self, device, payload):
        profile = str(device["port_id"])
        self._sync_pending[profile] = self._sync_pending.get(profile, 0) + 1
        self.controller.pause_port(profile)
        def completed():
            self._sync_pending[profile] -= 1
            active_here = self._registration_active and getattr(self, "_registration_profile", None) == profile
            if not self._sync_pending[profile] and not active_here and profile not in self._sync_failed:
                self.controller.resume_port(profile)
        def failed(message):
            self._sync_failed.add(profile)
            completed()
            self.repo.error.emit(tr("分机登记已读取，但电脑档案同步失败；本串口暂停采集，请重新读取登记表重试。{error}",
                                    error=message))
        def synced(_):
            def loaded(_):
                self._sync_failed.discard(profile)
                self.controller.log.emit(tr("{name}：已同步 {count} 个登记点（配置代次 {generation}）",
                                            name=device["name"], count=len(payload["bindings"]),
                                            generation=payload["generation"]))
                completed()
            self.controller.reload_metadata(callback=loaded, on_error=failed)
        self.repo.call("sync_verified_bindings", device["id"], payload["bindings"], payload["generation"], callback=synced, on_error=failed)

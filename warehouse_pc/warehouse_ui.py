"""Local warehouse registry matching the original Vue query/table structure."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QSizePolicy,
)
from .domain import configured_point_keys


class WarehousePage(QWidget):
    def __init__(self, repo, controller, parent=None):
        super().__init__(parent)
        self.repo, self.controller = repo, controller
        self._busy = False
        self._keyword = ""
        self._rows = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(12)
        header = QHBoxLayout()
        title = QLabel("仓房信息管理")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch()
        mode = QLabel("模拟档案" if controller.demo else "本地仓房档案")
        mode.setObjectName("muted")
        header.addWidget(mode)
        layout.addLayout(header)

        query_card = QFrame()
        query_card.setObjectName("section")
        query_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        query = QVBoxLayout(query_card)
        query.setContentsMargins(12, 10, 12, 12)
        query.setSpacing(8)
        query_title = QLabel("查询")
        query_title.setObjectName("cardTitle")
        query.addWidget(query_title)
        row = QHBoxLayout()
        row.addWidget(QLabel("仓房名称"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("请输入仓房名称")
        self.search.returnPressed.connect(self.apply_search)
        row.addWidget(self.search, 1)
        self.query_button, self.clear_button, self.add_button = QPushButton("查询"), QPushButton("清空"), QPushButton("添加仓房")
        self.query_button.setObjectName("primary")
        self.add_button.setObjectName("primary")
        self.query_button.clicked.connect(self.apply_search)
        self.clear_button.clicked.connect(self.clear_search)
        self.add_button.clicked.connect(self.add_warehouse)
        for button in (self.query_button, self.clear_button, self.add_button):
            row.addWidget(button)
        query.addLayout(row)
        layout.addWidget(query_card)

        table_card = QFrame()
        table_card.setObjectName("section")
        table_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        body = QVBoxLayout(table_card)
        body.setContentsMargins(12, 10, 12, 12)
        body.setSpacing(8)
        row = QHBoxLayout()
        heading = QLabel("仓房信息管理")
        heading.setObjectName("cardTitle")
        row.addWidget(heading)
        row.addStretch()
        self.count_label = QLabel("0 条记录")
        self.count_label.setObjectName("muted")
        row.addWidget(self.count_label)
        self.refresh_button = QPushButton("刷新列表")
        self.refresh_button.clicked.connect(self.reload)
        row.addWidget(self.refresh_button)
        body.addLayout(row)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["仓房名称", "关联分机数", "粮温点数", "仓温湿点数", "档案状态", "操作"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setMinimumSectionSize(100)
        body.addWidget(self.table)
        self.empty_label = QLabel("尚无仓房，请点击“添加仓房”创建本地档案。")
        self.empty_label.setObjectName("muted")
        body.addWidget(self.empty_label)
        note = QLabel("测点数量是所关联分机的配置容量。已有分机的仓房不能删除，请先在测温分机管理中处理关联；删除空仓房不会清理历史。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        body.addWidget(note)
        layout.addWidget(table_card)
        self.status = QLabel("就绪")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()
        controller.metadata_changed.connect(self.refresh)
        self.refresh()

    def _set_busy(self, value, text=""):
        self._busy = value
        for widget in (self.search, self.query_button, self.clear_button, self.add_button, self.refresh_button, self.table):
            widget.setEnabled(not value)
        if text:
            self.status.setText(text)

    def _error(self, message):
        if "FOREIGN KEY" in message.upper():
            message = "该仓房仍有关联分机，不能删除。请先到测温分机管理中处理关联。"
        elif "UNIQUE" in message.upper():
            message = "仓房名称已存在，请使用其他名称。"
        self._set_busy(False, message)
        QMessageBox.warning(self, "仓房操作未完成", message)

    def apply_search(self):
        self._keyword = self.search.text().strip().casefold()
        self.refresh()

    def clear_search(self):
        self.search.clear()
        self.apply_search()

    def refresh(self, *_):
        metadata = self.controller.metadata
        self._rows = []
        for warehouse in metadata["warehouses"]:
            if self._keyword not in warehouse["name"].casefold():
                continue
            devices = [row for row in metadata["devices"] if row["warehouse_id"] == warehouse["id"]]
            self._rows.append({**warehouse, "device_count": len(devices),
                               "grain": sum(sum(k == "grain" for k, p in configured_point_keys(row)) for row in devices),
                               "ambient": sum(sum(k == "ambient" for k, p in configured_point_keys(row)) for row in devices)})
        self.table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            for column, value in enumerate((row["name"], row["device_count"], row["grain"], row["ambient"], "已关联设备" if row["device_count"] else "空仓房档案")):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))
            actions = QWidget()
            buttons = QHBoxLayout(actions)
            buttons.setContentsMargins(4, 0, 4, 0)
            buttons.setSpacing(4)
            rename, delete = QPushButton("重命名"), QPushButton("删除")
            for button in (rename, delete):
                button.setMaximumHeight(26)
                button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            delete.setObjectName("danger")
            delete.setToolTip("仅允许删除没有关联分机的空仓房")
            rename.clicked.connect(lambda _, identifier=row["id"]: self.rename_warehouse(identifier))
            delete.clicked.connect(lambda _, identifier=row["id"]: self.delete_warehouse(identifier))
            buttons.addWidget(rename, alignment=Qt.AlignmentFlag.AlignLeft)
            buttons.addWidget(delete, alignment=Qt.AlignmentFlag.AlignLeft)
            buttons.addStretch()
            self.table.setCellWidget(index, 5, actions)
        self.table.setColumnWidth(5, 160)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.table.setFixedHeight(min(440, max(100, self.table.horizontalHeader().height() + len(self._rows) * 30 + 6)))
        self.count_label.setText(f"{len(self._rows)} 条记录")
        self.empty_label.setVisible(not self._rows)
        self.empty_label.setText("当前名称条件下没有仓房。" if metadata["warehouses"] else "尚无仓房，请点击“添加仓房”创建本地档案。")

    def reload(self):
        if self._busy:
            return
        self._set_busy(True, "正在读取本地仓房档案…")
        self.controller.reload_metadata(callback=lambda _: self._set_busy(False, "仓房列表已刷新"), on_error=self._error)

    def _mutate(self, method, *args):
        self._set_busy(True, "正在保存仓房档案…")

        def saved(_):
            self.controller.reload_metadata(callback=lambda _: self._set_busy(False, "仓房档案已更新"), on_error=self._error)
        self.repo.call(method, *args, callback=saved, on_error=self._error)

    def add_warehouse(self):
        if self._busy:
            return
        name, accepted = QInputDialog.getText(self, "添加仓房", "仓房名称（不超过 200 字）：")
        if accepted:
            name = name.strip()
            if not name or len(name) > 200:
                self._error("仓房名称不能为空且不能超过 200 字。")
                return
            self._mutate("save_warehouse", name)

    def rename_warehouse(self, identifier):
        if self._busy:
            return
        warehouse = next((row for row in self.controller.metadata["warehouses"] if row["id"] == identifier), None)
        if not warehouse:
            self._error("仓房已不存在，请刷新列表。")
            return
        name, accepted = QInputDialog.getText(self, "重命名仓房", "新的仓房名称：", text=warehouse["name"])
        if accepted:
            name = name.strip()
            if not name or len(name) > 200:
                self._error("仓房名称不能为空且不能超过 200 字。")
                return
            self._mutate("save_warehouse", name, identifier)

    def delete_warehouse(self, identifier):
        if self._busy:
            return
        warehouse = next((row for row in self.controller.metadata["warehouses"] if row["id"] == identifier), None)
        if not warehouse:
            self._error("仓房已不存在，请刷新列表。")
            return
        if any(row["warehouse_id"] == identifier for row in self.controller.metadata["devices"]):
            self._error("该仓房仍有关联分机，不能删除。请先到测温分机管理中处理关联。")
            return
        if QMessageBox.question(self, "删除空仓房档案", f"删除空仓房“{warehouse['name']}”？该操作仅移除此仓房档案。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._mutate("delete_warehouse", identifier)

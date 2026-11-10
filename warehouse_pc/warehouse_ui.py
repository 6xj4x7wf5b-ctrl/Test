"""Local warehouse registry matching the original Vue query/table structure."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QMessageBox, QPushButton, QMenu, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QSizePolicy,
)
from .domain import configured_point_keys
from .filter_panel import FilterPanel
from .i18n import tr


class WarehousePage(QWidget):
    def __init__(self, repo, controller, parent=None):
        super().__init__(parent)
        self.repo, self.controller = repo, controller
        self._busy = False
        self._keyword = ""
        self._rows = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        query_card = FilterPanel()
        self.search = QLineEdit()
        self.search.setAccessibleName(tr('仓房名称'))
        self.search.setPlaceholderText(tr('搜索仓房名称'))
        self.search.returnPressed.connect(self.apply_search)
        query_card.add_field(tr('仓房名称'), self.search)
        self.query_button, self.clear_button, self.add_button = QPushButton(tr('查询')), QPushButton(tr('清空')), QPushButton(tr('新增仓房'))
        self.query_button.setObjectName("primary")
        self.add_button.setObjectName("primary")
        self.query_button.clicked.connect(self.apply_search)
        self.clear_button.clicked.connect(self.clear_search)
        self.add_button.clicked.connect(self.add_warehouse)
        query_card.add_action(self.query_button)
        query_card.add_action(self.clear_button)
        self.refresh_button = QPushButton(tr('刷新'))
        self.refresh_button.clicked.connect(self.reload)
        layout.addWidget(query_card)

        table_card = QFrame()
        table_card.setObjectName("section")
        table_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        body = QVBoxLayout(table_card)
        body.setContentsMargins(12, 10, 12, 12)
        body.setSpacing(8)
        row = QHBoxLayout()
        heading = QLabel(tr('仓房列表'))
        heading.setObjectName("sectionTitle")
        row.addWidget(heading)
        self.count_label = QLabel(tr('0 条记录'))
        self.count_label.setObjectName("sectionMeta")
        row.addWidget(self.count_label)
        row.addStretch()
        row.addWidget(self.refresh_button)
        row.addWidget(self.add_button)
        body.addLayout(row)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([tr('仓房名称'), tr('关联分机数'), tr('粮温点数'), tr('仓温湿点数'), tr('档案状态'), tr('操作')])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setMinimumSectionSize(100)
        body.addWidget(self.table)
        self.empty_label = QLabel(tr('尚无仓房，请点击“新增仓房”创建本地档案。'))
        self.empty_label.setObjectName("muted")
        body.addWidget(self.empty_label)
        self.table.setToolTip(tr('测点按当前已核验登记统计。有关联分机的仓房不能删除。'))
        layout.addWidget(table_card)
        self.status = QLabel("")
        self.status.hide()
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
            self.status.show()

    def _error(self, message):
        if "FOREIGN KEY" in message.upper():
            message = tr('该仓房仍有关联分机，不能删除。请先到测温分机管理中处理关联。')
        elif "UNIQUE" in message.upper():
            message = tr('仓房名称已存在，请使用其他名称。')
        self._set_busy(False, message)
        QMessageBox.warning(self, tr('仓房操作未完成'), message)

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
            for column, value in enumerate((row["name"], row["device_count"], row["grain"], row["ambient"], tr('已关联设备') if row["device_count"] else tr('未关联分机'))):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))
            actions = QWidget()
            buttons = QHBoxLayout(actions)
            buttons.setContentsMargins(4, 0, 4, 0)
            buttons.setSpacing(4)
            more = QPushButton(tr('更多'))
            more.setObjectName("quietButton")
            more.setMaximumHeight(26)
            menu = QMenu(more)
            menu.addAction(tr('重命名'), lambda checked=False, identifier=row["id"]: self.rename_warehouse(identifier))
            menu.addAction(tr('删除空仓房'), lambda checked=False, identifier=row["id"]: self.delete_warehouse(identifier))
            more.setMenu(menu)
            buttons.addWidget(more, alignment=Qt.AlignmentFlag.AlignLeft)
            buttons.addStretch()
            self.table.setCellWidget(index, 5, actions)
        self.table.setColumnWidth(5, 100)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        header_height = self.table.horizontalHeader().sizeHint().height()
        rows_height = self.table.verticalHeader().length()
        self.table.setFixedHeight(min(440, header_height + rows_height + 2 * self.table.frameWidth()))
        self.table.setVisible(bool(self._rows))
        self.count_label.setText(tr('{p0} 条记录', p0=len(self._rows)))
        self.empty_label.setVisible(not self._rows)
        self.empty_label.setText(tr('当前名称条件下没有仓房。') if metadata["warehouses"] else tr('尚无仓房，请点击“新增仓房”创建本地档案。'))

    def reload(self):
        if self._busy:
            return
        self._set_busy(True, tr('正在读取本地仓房档案…'))
        self.controller.reload_metadata(callback=lambda _: self._set_busy(False, tr('仓房列表已刷新')), on_error=self._error)

    def _mutate(self, method, *args):
        self._set_busy(True, tr('正在保存仓房档案…'))

        def saved(_):
            self.controller.reload_metadata(callback=lambda _: self._set_busy(False, tr('仓房档案已更新')), on_error=self._error)
        self.repo.call(method, *args, callback=saved, on_error=self._error)

    def add_warehouse(self):
        if self._busy:
            return
        name, accepted = QInputDialog.getText(self, tr('新增仓房'), tr('仓房名称（不超过 200 字）：'))
        if accepted:
            name = name.strip()
            if not name or len(name) > 200:
                self._error(tr('仓房名称不能为空且不能超过 200 字。'))
                return
            self._mutate("save_warehouse", name)

    def rename_warehouse(self, identifier):
        if self._busy:
            return
        warehouse = next((row for row in self.controller.metadata["warehouses"] if row["id"] == identifier), None)
        if not warehouse:
            self._error(tr('仓房已不存在，请刷新列表。'))
            return
        name, accepted = QInputDialog.getText(self, tr('重命名仓房'), tr('新的仓房名称：'), text=warehouse["name"])
        if accepted:
            name = name.strip()
            if not name or len(name) > 200:
                self._error(tr('仓房名称不能为空且不能超过 200 字。'))
                return
            self._mutate("save_warehouse", name, identifier)

    def delete_warehouse(self, identifier):
        if self._busy:
            return
        warehouse = next((row for row in self.controller.metadata["warehouses"] if row["id"] == identifier), None)
        if not warehouse:
            self._error(tr('仓房已不存在，请刷新列表。'))
            return
        if any(row["warehouse_id"] == identifier for row in self.controller.metadata["devices"]):
            self._error(tr('该仓房仍有关联分机，不能删除。请先到测温分机管理中处理关联。'))
            return
        if QMessageBox.question(self, tr('删除空仓房档案'), tr('删除空仓房“{p0}”？该操作仅移除此仓房档案。', p0=warehouse['name']),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._mutate("delete_warehouse", identifier)

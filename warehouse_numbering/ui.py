"""Compact local UI for the single-probe numbering workflow."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QScrollArea, QSpinBox, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QAbstractItemView,
)

from warehouse_pc.filter_panel import FilterPanel

NUMBERING_STYLE = """
QPushButton#primary:disabled { background:#e7ecf2; color:#8795a5; border-color:#c9d3df; }
QLabel#numberingValue { font-size:30px; font-weight:700; color:#25374a; }
QLabel#numberingStatus { background:#edf3fb; border:1px solid #d3e0ef; padding:10px; }
QLabel#numberingStatus[tone="success"] { background:#edf5f2; border-color:#bdd8ca; color:#376b51; }
QLabel#numberingStatus[tone="error"] { background:#fff2ee; border-color:#e2bbae; color:#9a4732; }
QLabel#numberingStatus[tone="pending"] { background:#faf4e9; border-color:#eadfc9; color:#87652e; }
QLabel#numberingStep { background:#f2f4f7; border-bottom:2px solid #d5dde5; padding:8px; }
QLabel#numberingStep[active="true"] { background:#eaf1fb; border-bottom-color:#2f6fdf; color:#245bc0; font-weight:600; }
QFrame#topbar QPushButton:checked { background:#2f6fdf; border-color:#578de8; color:white; }
"""

_STATUS_NAMES = {'verified': '已完成', 'pending': '待断电核验', 'failed': '未通过'}


def _button(text, callback, primary=False):
    button = QPushButton(text)
    button.setObjectName('primary' if primary else 'secondary')
    button.clicked.connect(callback)
    return button


def _section(title):
    frame = QFrame()
    frame.setObjectName('section')
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 14)
    layout.setSpacing(10)
    heading = QHBoxLayout()
    label = QLabel(title)
    label.setObjectName('sectionTitle')
    heading.addWidget(label)
    heading.addStretch()
    layout.addLayout(heading)
    return frame, layout, heading


class ConnectionDialog(QDialog):
    def __init__(self, values, parent=None):
        super().__init__(parent)
        self.setWindowTitle('连接设置')
        self.setMinimumWidth(320)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.address = QSpinBox()
        self.address.setRange(1, 247)
        self.address.setValue(values['address'])
        self.baud = QComboBox()
        for value in (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200):
            self.baud.addItem(str(value), value)
        self.baud.setCurrentIndex(self.baud.findData(values['baud']))
        self.parity = QComboBox()
        for label, value in (('无校验 N', 'N'), ('偶校验 E', 'E'), ('奇校验 O', 'O')):
            self.parity.addItem(label, value)
        self.parity.setCurrentIndex(self.parity.findData(values['parity']))
        self.stop_bits = QComboBox()
        self.stop_bits.addItem('1', 1)
        self.stop_bits.addItem('2', 2)
        self.stop_bits.setCurrentIndex(self.stop_bits.findData(values['stop_bits']))
        for title, field in (('工装地址', self.address), ('波特率', self.baud), ('校验位', self.parity), ('停止位', self.stop_bits)):
            form.addRow(title, field)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        return dict(address=self.address.value(), baud=self.baud.currentData(),
                    parity=self.parity.currentData(), stop_bits=self.stop_bits.currentData())


class NumberingWindow(QMainWindow):
    def __init__(self, service, data_dir: Path, *, demo=False):
        super().__init__()
        self.service, self.data_dir, self.demo = service, Path(data_dir), demo
        self.connection_settings = dict(address=1, baud=1200, parity='N', stop_bits=1)
        self._completed_ids = set()
        self._pending_id = None
        self._records = []
        self._closing = False
        self._label_text = ''
        self.setWindowTitle('GXHT3W 探头编号工具' + (' [模拟演示]' if demo else ''))
        self.setMinimumSize(1000, 700)
        self.resize(1100, 760)
        root = QWidget()
        root.setObjectName('canvas')
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(6, 6, 6, 0)
        layout.setSpacing(6)
        topbar = QFrame()
        topbar.setObjectName('topbar')
        top = QHBoxLayout(topbar)
        top.setContentsMargins(16, 14, 16, 14)
        brand = QLabel('GXHT3W 探头编号工具')
        brand.setObjectName('appTitle')
        top.addWidget(brand)
        top.addStretch()
        if demo:
            badge = QLabel('模拟演示')
            badge.setObjectName('demoBadge')
            top.addWidget(badge)
        self.nav_buttons = []
        for index, title in enumerate(('探头编号', '编号记录')):
            button = _button(title, lambda checked=False, value=index: self.set_page(value))
            button.setCheckable(True)
            self.nav_buttons.append(button)
            top.addWidget(button)
        layout.addWidget(topbar)
        surface = QFrame()
        surface.setObjectName('workArea')
        body = QVBoxLayout(surface)
        body.setContentsMargins(14, 14, 14, 14)
        body.setSpacing(12)
        body.addWidget(self._connection_panel())
        self.pages = QStackedWidget()
        self.pages.addWidget(self._numbering_page())
        self.pages.addWidget(self._records_page())
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.pages)
        body.addWidget(scroll, 1)
        layout.addWidget(surface, 1)
        self.statusBar().showMessage('本地保存 · 一次连接一只探头')
        service.state_changed.connect(self._state_changed)
        service.completed.connect(self._completed)
        service.records_changed.connect(self._reload_records)
        self._reload_ports()
        self._reload_records()
        self.set_page(0)
        self._state_changed(service.state)

    def _connection_panel(self):
        panel = FilterPanel()
        self.port = QComboBox()
        self.port.setEditable(True)
        self.port.setMinimumContentsLength(10)
        self.port.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        panel.add_field('串口', self.port, stretch=3)
        self.parameters = QLabel()
        self._show_parameters()
        panel.add_field('通信参数', self.parameters, stretch=2)
        self.connection_status = QLabel('未连接')
        panel.add_field('编号工装', self.connection_status, stretch=2)
        self.refresh_ports = _button('刷新串口', self._reload_ports)
        self.settings_button = _button('连接设置', self._connection_dialog)
        self.connect_button = _button('连接工装', self._toggle_connection)
        for button in (self.refresh_ports, self.settings_button, self.connect_button):
            panel.add_action(button)
        return panel

    def _numbering_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        cards = QHBoxLayout()
        cards.setSpacing(12)
        probe_card, probe, heading = _section('当前探头')
        self.read_button = _button('读取探头', self.service.read_probe)
        heading.addWidget(self.read_button)
        values = QHBoxLayout()
        self.probe_root, self.probe_point = QLabel('—'), QLabel('—')
        for label, value in (('根号', self.probe_root), ('点号', self.probe_point)):
            column = QVBoxLayout()
            title = QLabel(label)
            title.setObjectName('muted')
            value.setObjectName('numberingValue')
            column.addWidget(title)
            column.addWidget(value)
            values.addLayout(column, 1)
        probe.addLayout(values)
        self.probe_state = QLabel('尚未读取探头')
        self.probe_state.setObjectName('muted')
        self.probe_state.setWordWrap(True)
        probe.addWidget(self.probe_state)
        probe.addStretch()
        rom_row = QHBoxLayout()
        self.rom = QLabel('ROM  —')
        self.rom.setObjectName('muted')
        self.rom.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.rom.setWordWrap(True)
        rom_row.addWidget(self.rom, 1)
        self.copy_rom = _button('复制', self._copy_rom)
        rom_row.addWidget(self.copy_rom)
        probe.addLayout(rom_row)
        cards.addWidget(probe_card, 1)
        target_card, target, _ = _section('写入编号')
        self.batch = QComboBox()
        self.batch.setEditable(True)
        self.batch.lineEdit().setPlaceholderText('例如：1号仓 / 分机1')
        self.batch.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        form = QFormLayout()
        form.setSpacing(9)
        form.addRow('目标批次', self.batch)
        numbering = QHBoxLayout()
        self.root_number, self.point_number = QSpinBox(), QSpinBox()
        for label, field in (('根号', self.root_number), ('点号', self.point_number)):
            field.setRange(1, 254)
            field.setValue(1)
            field.setAccessibleName('待写' + label)
            numbering.addWidget(QLabel(label))
            numbering.addWidget(field, 1)
        form.addRow(numbering)
        self.advance = QCheckBox('完成断电复核后，点号加 1')
        self.advance.setChecked(True)
        form.addRow(self.advance)
        target.addLayout(form)
        self.write_button = _button('写入并校验', self._write, True)
        self.write_button.setMinimumHeight(32)
        target.addWidget(self.write_button)
        self.renumber_button = _button('给当前探头改号…', self._renumber)
        target.addWidget(self.renumber_button, 0, Qt.AlignmentFlag.AlignRight)
        cards.addWidget(target_card, 1)
        layout.addLayout(cards)
        process_card, process, _ = _section('过程与结果')
        steps = QHBoxLayout()
        steps.setSpacing(6)
        self.steps = []
        for text in ('1 读取探头', '2 写入编号', '3 回读校验', '4 断电复核'):
            step = QLabel(text)
            step.setObjectName('numberingStep')
            steps.addWidget(step, 1)
            self.steps.append(step)
        process.addLayout(steps)
        self.feedback = QLabel()
        self.feedback.setWordWrap(True)
        self.feedback.setObjectName('numberingStatus')
        self.feedback.setMinimumHeight(50)
        process.addWidget(self.feedback)
        actions = QHBoxLayout()
        self.verify_button = _button('开始断电复核', self.service.verify_power_cycle, True)
        actions.addWidget(self.verify_button)
        self.pause_verify_button = _button('暂停核验', self.service.stop_verification)
        actions.addWidget(self.pause_verify_button)
        self.defer_button = _button('稍后核验', self.service.defer_record)
        self.defer_button.setToolTip('保留待核验记录和已占用编号，换下一只探头继续。')
        actions.addWidget(self.defer_button)
        self.label = QLabel()
        self.label.setObjectName('sectionTitle')
        actions.addWidget(self.label, 1)
        self.copy_label = _button('复制标签文字', self._copy_label)
        actions.addWidget(self.copy_label)
        process.addLayout(actions)
        layout.addWidget(process_card)
        self.demo_controls = QWidget()
        demo_row = QHBoxLayout(self.demo_controls)
        demo_row.setContentsMargins(0, 0, 0, 0)
        demo_title = QLabel('模拟操作')
        demo_title.setObjectName('muted')
        demo_row.addWidget(demo_title)
        self.remove_button = _button('拔下探头', self.service.demo_remove)
        self.insert_button = _button('重新接入', lambda: self.service.demo_insert(new_probe=False))
        self.next_button = _button('换下一只', lambda: self.service.demo_insert(new_probe=True))
        self.restore_pending_button = _button('接回待核验探头', lambda: self.service.demo_restore_pending())
        for button in (self.remove_button, self.insert_button, self.next_button, self.restore_pending_button):
            demo_row.addWidget(button)
        demo_row.addStretch()
        self.demo_controls.setVisible(self.demo)
        layout.addWidget(self.demo_controls)
        layout.addStretch()
        self.batch.currentTextChanged.connect(self._update_actions)
        self.root_number.valueChanged.connect(self._update_actions)
        self.point_number.valueChanged.connect(self._update_actions)
        return page

    def _records_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        filters = FilterPanel()
        self.record_batch = QComboBox()
        self.record_status = QComboBox()
        self.record_status.addItem('全部状态', None)
        for key, name in _STATUS_NAMES.items():
            self.record_status.addItem(name, key)
        filters.add_field('编号批次', self.record_batch)
        filters.add_field('核验状态', self.record_status)
        filters.add_action(_button('刷新', self._reload_records))
        filters.add_action(_button('导出批次 CSV', self._export))
        layout.addWidget(filters)
        section, table_layout, heading = _section('编号记录')
        self.record_count = QLabel('0 条')
        self.record_count.setObjectName('sectionMeta')
        heading.addWidget(self.record_count)
        self.records_table = QTableWidget(0, 6)
        self.records_table.setHorizontalHeaderLabels(['更新时间', '批次', '探头 ROM', '原编号 → 新编号', '状态', '备注'])
        self.records_table.verticalHeader().hide()
        self.records_table.verticalHeader().setDefaultSectionSize(30)
        self.records_table.setAlternatingRowColors(True)
        self.records_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.records_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.records_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.records_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.records_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        table_layout.addWidget(self.records_table, 1)
        self.records_empty = QLabel('暂无编号记录')
        self.records_empty.setObjectName('muted')
        table_layout.addWidget(self.records_empty)
        actions = QHBoxLayout()
        self.resume_button = _button('继续核验所选记录', self._resume)
        actions.addWidget(self.resume_button)
        actions.addStretch()
        self.record_copy = _button('复制所选 ROM', self._copy_record_rom)
        actions.addWidget(self.record_copy)
        table_layout.addLayout(actions)
        layout.addWidget(section, 1)
        self.record_feedback = QLabel()
        self.record_feedback.setObjectName('muted')
        self.record_feedback.setWordWrap(True)
        layout.addWidget(self.record_feedback)
        self.record_batch.currentIndexChanged.connect(self._render_records)
        self.record_status.currentIndexChanged.connect(self._render_records)
        self.records_table.itemSelectionChanged.connect(self._update_actions)
        return page

    def set_page(self, index):
        self.pages.setCurrentIndex(index)
        for current, button in enumerate(self.nav_buttons):
            button.setChecked(current == index)
        if index == 1:
            self._reload_records()

    def _show_parameters(self):
        settings = self.connection_settings
        self.parameters.setText(f"{settings['baud']} / 8{settings['parity']}{settings['stop_bits']} · 地址 {settings['address']}")

    def _connection_dialog(self):
        dialog = ConnectionDialog(self.connection_settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.connection_settings = dialog.values()
            self._show_parameters()

    def _reload_ports(self):
        previous = self.port.currentText()
        choices = ['模拟工装'] if self.demo else self.service.available_ports()
        self.port.clear()
        self.port.addItems(choices)
        if previous:
            self.port.setCurrentText(previous)

    def _toggle_connection(self):
        if self.service.state.connected:
            self.service.disconnect_fixture()
        else:
            self.service.connect_fixture(self.port.currentText().strip(), **self.connection_settings)

    def _state_changed(self, state):
        probe = state.probe
        self.connect_button.setText('断开连接' if state.connected else '连接工装')
        self.connection_status.setText('工装已确认' if state.fixture else '连接待确认' if state.connected else '未连接')
        self.probe_root.setText(str(probe.root) if probe else '—')
        self.probe_point.setText(str(probe.point) if probe else '—')
        self.rom.setText('ROM  ' + probe.rom_hex if probe else 'ROM  —')
        record = self.service.verified_assignment() if probe else None
        matches = bool(record and probe and (record.root, record.point, record.configuration)
                       == (probe.root, probe.point, probe.configuration))
        self.probe_state.setText('已验证编号' if matches else '读值与已验证记录不符' if record
                                else '已读取 · 编号未核验' if probe else '尚未读取探头')
        if state.pending and state.pending.id != self._pending_id:
            self._pending_id = state.pending.id
            self.batch.setCurrentText(state.pending.batch)
            self.root_number.setValue(state.pending.root)
            self.point_number.setValue(state.pending.point)
        if state.pending is None:
            self._pending_id = None
        self.feedback.setText(state.message or '连接编号工装后，接入一只探头并读取。')
        tone = 'success' if state.phase == 'verified' else 'error' if state.phase == 'error' else 'pending' if state.pending else 'normal'
        self.feedback.setProperty('tone', tone)
        self.feedback.style().unpolish(self.feedback)
        self.feedback.style().polish(self.feedback)
        step = {'reading': 0, 'ready': 0, 'writing': 1, 'pending': 2, 'wait_remove': 3, 'wait_insert': 3, 'verified': 3}.get(state.phase, -1)
        for index, label in enumerate(self.steps):
            label.setProperty('active', index == step)
            label.style().unpolish(label)
            label.style().polish(label)
        if state.phase != 'verified':
            self._label_text = ''
            self.label.clear()
        self._update_actions()

    def _update_actions(self, *_):
        if not hasattr(self, 'records_table'):
            return
        state = self.service.state
        locked = state.busy or state.pending is not None
        for field in (self.batch, self.root_number, self.point_number, self.advance):
            field.setEnabled(not locked)
        for field in (self.port, self.refresh_ports, self.settings_button):
            field.setEnabled(not state.connected and not state.busy)
        self.connect_button.setEnabled(not state.busy)
        self.read_button.setEnabled(state.fixture and not locked)
        self.copy_rom.setEnabled(state.probe is not None)
        known = self.service.verified_assignment() if state.probe else None
        writable = (state.fixture and state.probe is not None and not locked
                    and state.phase in {'ready', 'verified'} and bool(self.batch.currentText().strip()))
        self.write_button.setEnabled(writable and known is None and state.phase != 'verified')
        self.write_button.setText('正在写入…' if state.phase == 'writing' else '等待断电复核' if state.pending
                                  else '已完成，请换下一只' if state.phase == 'verified'
                                  else '当前探头已有编号' if known else '写入并校验')
        self.renumber_button.setVisible(known is not None or state.phase == 'verified')
        self.renumber_button.setEnabled(writable)
        waiting = state.phase in {'wait_remove', 'wait_insert'}
        self.verify_button.setVisible(state.pending is not None and not waiting)
        self.pause_verify_button.setVisible(waiting)
        self.defer_button.setVisible(state.pending is not None)
        self.defer_button.setEnabled(state.pending is not None and not state.busy and not waiting)
        self.verify_button.setEnabled(state.fixture and state.pending is not None and not state.busy)
        self.copy_label.setVisible(bool(self._label_text))
        self.remove_button.setEnabled(state.fixture and state.phase != 'writing')
        self.insert_button.setEnabled(state.fixture and state.phase != 'writing')
        self.next_button.setEnabled(state.fixture and not locked)
        self.restore_pending_button.setEnabled(self.demo and state.fixture and state.pending is not None and not state.busy)
        record = self._selected_record()
        self.resume_button.setEnabled(bool(record and record.status == 'pending') and state.fixture and not state.busy and state.pending is None)
        self.record_copy.setEnabled(record is not None)

    def _write(self):
        if self.write_button.isEnabled():
            self.service.write_number(self.batch.currentText().strip(), self.root_number.value(), self.point_number.value(),
                                      advance=self.advance.isChecked())

    def _renumber(self):
        if not self.renumber_button.isEnabled():
            return
        probe = self.service.state.probe
        if probe is None:
            return
        answer = QMessageBox.question(self, '确认修改当前探头编号',
            f'当前探头：{probe.rom_hex}\n原编号：根 {probe.root} / 点 {probe.point}\n'
            f'新编号：根 {self.root_number.value()} / 点 {self.point_number.value()}\n'
            f'目标批次：{self.batch.currentText().strip()}\n\n确认给这只探头改号？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.service.write_number(self.batch.currentText().strip(), self.root_number.value(), self.point_number.value(),
                                      renumber=True, advance=self.advance.isChecked())

    def _completed(self, record):
        if record.id in self._completed_ids:
            return
        self._completed_ids.add(record.id)
        self._label_text = f'根 {record.root:02d} / 点 {record.point:02d}'
        self.label.setText('编号完成 · ' + self._label_text)
        if record.next_point is not None:
            self.point_number.setValue(record.next_point)
        elif record.advance and record.point >= 254:
            self.statusBar().showMessage('本组已到点号 254，请选择下一组。')
        self._reload_records()
        self._update_actions()

    def _reload_records(self):
        selected_batch = self.batch.currentText()
        self.batch.blockSignals(True)
        self.batch.clear()
        self.batch.addItems(self.service.batches())
        self.batch.setCurrentText(selected_batch or ('演示批次' if self.demo else ''))
        self.batch.blockSignals(False)
        selected_filter = self.record_batch.currentData()
        self.record_batch.blockSignals(True)
        self.record_batch.clear()
        self.record_batch.addItem('全部批次', None)
        for batch in self.service.batches():
            self.record_batch.addItem(batch, batch)
        self.record_batch.setCurrentIndex(max(0, self.record_batch.findData(selected_filter)))
        self.record_batch.blockSignals(False)
        self._render_records()

    def _render_records(self, *_):
        selected = self._selected_record()
        self._records = [record for record in self.service.records(self.record_batch.currentData())
                         if self.record_status.currentData() in (None, record.status)]
        self.records_table.setRowCount(len(self._records))
        self.records_table.clearSelection()
        for row, record in enumerate(self._records):
            try:
                local_time = datetime.fromisoformat(record.updated_at).astimezone().strftime('%Y-%m-%d %H:%M:%S')
            except ValueError:
                local_time = record.updated_at
            values = (local_time, record.batch, record.rom_hex,
                      f'{record.old_root}/{record.old_point} → {record.root}/{record.point}',
                      _STATUS_NAMES.get(record.status, record.status), record.note)
            for column, text in enumerate(values):
                item = QTableWidgetItem(str(text))
                item.setData(Qt.ItemDataRole.UserRole, record.id)
                item.setToolTip(record.updated_at if column == 0 else str(text))
                self.records_table.setItem(row, column, item)
            if selected and selected.id == record.id:
                self.records_table.selectRow(row)
        self.record_count.setText(f'{len(self._records)} 条')
        self.records_empty.setVisible(not self._records)
        self._update_actions()

    def _selected_record(self):
        selected = self.records_table.selectedItems()
        identifier = selected[0].data(Qt.ItemDataRole.UserRole) if selected else None
        return next((record for record in self._records if record.id == identifier), None)

    def _resume(self):
        record = self._selected_record()
        if record and self.resume_button.isEnabled():
            self.set_page(0)
            self.service.resume_record(record.id)

    def _copy_rom(self):
        probe = self.service.state.probe
        if probe:
            QApplication.clipboard().setText(probe.rom_hex)
            self.statusBar().showMessage('探头 ROM 已复制', 3000)

    def _copy_label(self):
        if self._label_text:
            QApplication.clipboard().setText(self._label_text)
            self.statusBar().showMessage('标签文字已复制', 3000)

    def _copy_record_rom(self):
        record = self._selected_record()
        if record:
            QApplication.clipboard().setText(record.rom_hex)
            self.record_feedback.setText('所选 ROM 已复制')

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, '导出编号记录', str(self.data_dir / '编号记录.csv'), 'CSV (*.csv)')
        if path:
            try:
                count = self.service.export_csv(Path(path), batch=self.record_batch.currentData())
            except (OSError, ValueError) as error:
                self.record_feedback.setText(f'导出失败：{error}')
            else:
                self.record_feedback.setText(f'已导出当前批次的全部状态记录：{count} 条 · {path}')

    def closeEvent(self, event):
        if self._closing:
            event.accept()
            return
        if self.service.state.phase in {'wait_remove', 'wait_insert'}:
            answer = QMessageBox.question(self, '保留待核验记录', '停止本次拔插核验并退出？记录会保留，下次可从编号记录继续核验。',
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        elif self.service.state.busy:
            QMessageBox.information(self, '操作进行中', '请等待当前操作完成。未完成断电复核的记录会保留，下次可继续核验。')
            event.ignore()
            return
        self._closing = True
        self.service.close()
        event.accept()

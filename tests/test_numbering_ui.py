"""Operator workflow tests: UI must not silently renumber or alter pending targets."""
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox

from warehouse_numbering.ui import NumberingWindow


@dataclass
class Record:
    id: int = 1
    batch: str = '1号仓 / 分机1'
    rom_hex: str = '2C EE 49 E1 8A AC 11 64'
    old_root: int = 75
    old_point: int = 70
    root: int = 1
    point: int = 3
    status: str = 'verified'
    created_at: str = '2026-11-13 09:00:00'
    updated_at: str = '2026-11-13 09:00:03'
    note: str = ''
    advance: bool = True
    configuration: int = 0x7F

    @property
    def next_point(self):
        return self.point + 1 if self.status == 'verified' and self.advance and self.point < 254 else None


class Service(QObject):
    state_changed = Signal(object)
    completed = Signal(object)
    records_changed = Signal()

    def __init__(self):
        super().__init__()
        self.probe = SimpleNamespace(root=75, point=70, rom_hex=Record().rom_hex, configuration=0x7F)
        self.state = SimpleNamespace(phase='ready', message='已读取一只探头', connected=True,
                                     fixture=True, probe=self.probe, pending=None, busy=False)
        self.calls = []
        self.rows = []
        self.assignment = None

    def available_ports(self):
        return ['COM3']

    def batches(self):
        return list(dict.fromkeys(row.batch for row in self.rows))

    def records(self, batch=None):
        return [row for row in self.rows if batch in (None, row.batch)]

    def verified_assignment(self):
        return self.assignment

    def connect_fixture(self, *args, **kwargs):
        self.calls.append(('connect', args, kwargs))

    def disconnect_fixture(self):
        self.calls.append(('disconnect',))

    def read_probe(self):
        self.calls.append(('read',))

    def write_number(self, *args, **kwargs):
        self.calls.append(('write', args, kwargs))

    def verify_power_cycle(self):
        self.calls.append(('verify',))

    def defer_record(self):
        self.calls.append(('defer',))

    def stop_verification(self):
        self.calls.append(('stop_verify',))

    def resume_record(self, identifier):
        self.calls.append(('resume', identifier))

    def demo_remove(self):
        self.calls.append(('remove',))

    def demo_insert(self, **kwargs):
        self.calls.append(('insert', kwargs))

    def demo_restore_pending(self):
        self.calls.append(('restore_pending',))

    def close(self):
        self.calls.append(('close',))


def test_verified_probe_requires_explicit_renumber_after_auto_increment(qt_application, tmp_path):
    service = Service()
    window = NumberingWindow(service, tmp_path)
    window.batch.setCurrentText('1号仓 / 分机1')
    window.point_number.setValue(3)
    window.write_button.click()
    assert service.calls == [('write', ('1号仓 / 分机1', 1, 3), {'advance': True})]
    service.assignment = record = Record()
    service.rows = [record]
    service.state.phase = 'verified'
    service.probe.root, service.probe.point = 1, 3
    service.state_changed.emit(service.state)
    service.completed.emit(record)
    assert window.point_number.value() == 4
    assert not window.write_button.isEnabled()
    window.write_button.click()
    assert len(service.calls) == 1
    with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
        window.renumber_button.click()
    assert len(service.calls) == 1
    with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
        window.renumber_button.click()
    assert service.calls[-1] == ('write', ('1号仓 / 分机1', 1, 4), {'renumber': True, 'advance': True})
    # A repeated completion notification must not advance a user's newly selected number.
    window.point_number.setValue(8)
    service.completed.emit(record)
    assert window.point_number.value() == 8
    window.close()


def test_pending_operation_locks_batch_number_and_uses_saved_record_for_recovery(qt_application, tmp_path):
    service = Service()
    record = Record(status='pending')
    service.rows = [record]
    window = NumberingWindow(service, tmp_path)
    service.state.pending = record
    service.state.phase = 'pending'
    service.state_changed.emit(service.state)
    assert window.batch.currentText() == record.batch
    assert (window.root_number.value(), window.point_number.value()) == (record.root, record.point)
    assert not window.batch.isEnabled() and not window.root_number.isEnabled() and not window.point_number.isEnabled()
    assert not window.write_button.isEnabled()
    window.verify_button.click()
    assert service.calls == [('verify',)]
    service.state.pending = None
    service.state.phase = 'ready'
    service.state_changed.emit(service.state)
    window.set_page(1)
    window.records_table.selectRow(0)
    window.resume_button.click()
    assert service.calls[-1] == ('resume', record.id)
    assert window.pages.currentIndex() == 0
    assert not any(call[0] == 'write' for call in service.calls)
    window.close()


def test_minimum_size_keeps_primary_workflow_controls_visible(qt_application, tmp_path):
    service = Service()
    window = NumberingWindow(service, tmp_path, demo=True)
    window.resize(1000, 700)
    window.show()
    qt_application.processEvents()
    assert window.size().width() == 1000
    for widget in (window.write_button, window.batch, window.root_number, window.point_number,
                   window.connect_button, window.read_button, window.feedback):
        assert widget.isVisible()
        origin = widget.mapTo(window, widget.rect().topLeft())
        assert 0 <= origin.x() < window.width()
        assert origin.x() + widget.width() <= window.width()
    window.close()


def test_known_rom_with_changed_readback_is_not_presented_as_verified(qt_application, tmp_path):
    service = Service()
    service.assignment = Record()
    service.probe.root, service.probe.point = 1, 3
    window = NumberingWindow(service, tmp_path)
    assert window.probe_state.text() == '已验证编号'
    service.probe.point = 4
    service.state_changed.emit(service.state)
    assert window.probe_state.text() == '读值与已验证记录不符'
    assert not window.write_button.isEnabled()
    service.probe.point = 3
    service.probe.configuration = 0x5F
    service.state_changed.emit(service.state)
    assert window.probe_state.text() == '读值与已验证记录不符'
    window.close()


def test_real_demo_ui_completes_power_cycle_before_advancing(qt_application, tmp_path, monkeypatch):
    from PySide6.QtTest import QTest
    from warehouse_numbering import service as backend
    from time import monotonic

    monkeypatch.setattr(backend, 'POWER_OFF_SECONDS', 0.05)
    monkeypatch.setattr(backend, 'POLL_INTERVAL_MS', 20)
    service = backend.NumberingService(tmp_path, demo=True)
    window = NumberingWindow(service, tmp_path, demo=True)

    def wait_for(predicate):
        deadline = monotonic() + 3
        while not predicate() and monotonic() < deadline:
            QTest.qWait(10)
        assert predicate(), service.state

    try:
        window.connect_button.click()
        wait_for(lambda: service.state.fixture and not service.state.busy)
        window.read_button.click()
        wait_for(lambda: service.state.probe is not None and not service.state.busy)
        window.root_number.setValue(2)
        window.point_number.setValue(9)
        window.write_button.click()
        wait_for(lambda: service.state.phase == 'pending')
        assert window.point_number.value() == 9
        assert not window.root_number.isEnabled()
        window.verify_button.click()
        wait_for(lambda: service.state.phase == 'wait_remove' and service.state.probe is not None)
        window.remove_button.click()
        wait_for(lambda: service.state.phase == 'wait_insert')
        assert window.point_number.value() == 9
        window.insert_button.click()
        wait_for(lambda: service.state.phase == 'verified')
        assert window.point_number.value() == 10
        assert '根 02 / 点 09' in window.label.text()
        assert not window.write_button.isEnabled()
        first_rom = service.state.probe.rom_hex
        window.next_button.click()
        wait_for(lambda: service.state.phase == 'ready' and service.state.probe is not None)
        assert service.state.probe.rom_hex != first_rom
        assert window.write_button.isEnabled()
        assert [(row.root, row.point, row.status) for row in service.records()] == [(2, 9, 'verified')]
    finally:
        service.close()
        window._closing = True
        window.close()


def test_demo_pending_restore_is_only_available_for_idle_pending_work(qt_application, tmp_path):
    service = Service()
    window = NumberingWindow(service, tmp_path, demo=True)
    window.show()
    assert not window.restore_pending_button.isEnabled()
    service.state.pending = Record(status='pending')
    service.state.phase = 'pending'
    service.state_changed.emit(service.state)
    assert window.restore_pending_button.isVisible() and window.restore_pending_button.isEnabled()
    window.restore_pending_button.click()
    assert service.calls == [('restore_pending',)]
    service.state.busy = True
    service.state.phase = 'wait_remove'
    service.state_changed.emit(service.state)
    assert not window.restore_pending_button.isEnabled()
    service.state.busy = False
    service.state.phase = 'pending'
    window.close()
    normal = NumberingWindow(Service(), tmp_path, demo=False)
    normal.show()
    assert not normal.restore_pending_button.isVisible()
    normal.close()


def test_demo_deferred_probe_can_be_restored_without_skipping_power_verification(qt_application, tmp_path, monkeypatch):
    from PySide6.QtTest import QTest
    from warehouse_numbering import service as backend
    from time import monotonic

    monkeypatch.setattr(backend, 'POWER_OFF_SECONDS', 0.05)
    monkeypatch.setattr(backend, 'POLL_INTERVAL_MS', 20)
    service = backend.NumberingService(tmp_path, demo=True)
    window = NumberingWindow(service, tmp_path, demo=True)

    def wait_for(predicate):
        deadline = monotonic() + 3
        while not predicate() and monotonic() < deadline:
            QTest.qWait(10)
        assert predicate(), service.state

    try:
        window.connect_button.click()
        wait_for(lambda: service.state.fixture and not service.state.busy)
        window.read_button.click()
        wait_for(lambda: service.state.probe is not None and not service.state.busy)
        original_rom = service.state.probe.rom_hex
        window.point_number.setValue(7)
        window.write_button.click()
        wait_for(lambda: service.state.phase == 'pending')
        window.defer_button.click()
        assert service.state.pending is None
        window.next_button.click()
        wait_for(lambda: service.state.probe is not None and not service.state.busy)
        assert service.state.probe.rom_hex != original_rom
        window.set_page(1)
        window.records_table.selectRow(0)
        window.resume_button.click()
        assert window.restore_pending_button.isEnabled()
        window.restore_pending_button.click()
        wait_for(lambda: service.state.probe is not None and not service.state.busy)
        assert service.state.probe.rom_hex == original_rom
        assert service.state.probe.point == 7
        assert service.state.phase == 'pending'
        assert service.records()[0].status == 'pending'
        window.verify_button.click()
        wait_for(lambda: service.state.phase == 'wait_remove' and service.state.probe is not None)
        window.remove_button.click()
        wait_for(lambda: service.state.phase == 'wait_insert')
        window.insert_button.click()
        wait_for(lambda: service.state.phase == 'verified')
        assert service.records()[0].status == 'verified'
        assert window.point_number.value() == 8
    finally:
        service.close()
        window._closing = True
        window.close()

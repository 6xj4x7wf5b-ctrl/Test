"""Verify write safety and recovery against the actual numbering state machine."""

import csv
import json
import time

from PySide6.QtTest import QTest

from warehouse_numbering import service as implementation
from warehouse_numbering.protocol import CAPABILITY, FixtureError, WRITE_NUMBER
from warehouse_numbering.service import NumberingService
from warehouse_pc.domain import crc8


def wait_for(predicate):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline and not predicate():
        QTest.qWait(5)
    assert predicate()


def connect(service):
    service.connect_fixture()
    wait_for(lambda: service.state.fixture)
    if service.state.pending is None:
        service.read_probe()
        wait_for(lambda: service.state.probe is not None)


def verify(service):
    service.verify_power_cycle()
    wait_for(lambda: service.state.probe is not None)
    service.demo_remove()
    wait_for(lambda: service.state.phase == "wait_insert")
    service.demo_insert()
    wait_for(lambda: not service.state.busy)


def fast_power_cycle(monkeypatch):
    monkeypatch.setattr(implementation, "POWER_OFF_SECONDS", 0.05)
    monkeypatch.setattr(implementation, "POLL_INTERVAL_MS", 10)


def test_unconfirmed_fixture_cannot_read_or_write(tmp_path, monkeypatch):
    service = NumberingService(tmp_path, demo=True)
    requests = []

    def production_board(_address, op, _payload, _token, callback):
        requests.append(op)
        callback(None, FixtureError("此设备未声明受支持的 GXHT3W 编号工装能力。"))

    try:
        monkeypatch.setattr(service.transport, "request", production_board)
        service.connect_fixture()
        assert service.state.connected and not service.state.fixture
        service.read_probe()
        service.write_number("Batch A", 2, 5)
        assert requests == [CAPABILITY]
        assert not service.records()
    finally:
        service.close()


def test_write_requires_real_absence_and_increments_once(tmp_path, monkeypatch):
    fast_power_cycle(monkeypatch)
    service = NumberingService(tmp_path, demo=True)
    completed = []
    service.completed.connect(completed.append)
    try:
        connect(service)
        assert service.state.probe.rom[0] == 0x2C
        service.write_number("Batch A", 1, 3)
        wait_for(lambda: not service.state.busy)
        assert service.transport.write_count == 1
        assert service.state.pending.status == "pending" and not completed
        service.verify_power_cycle()
        wait_for(lambda: service.state.probe is not None)
        QTest.qWait(140)
        assert service.state.phase == "wait_remove"
        assert not completed
        service.demo_remove()
        wait_for(lambda: service.state.phase == "wait_insert")
        service.demo_insert()
        wait_for(lambda: service.state.phase == "verified")
        assert completed[0].next_point == 4
        assert service.verified_number("batch a") == (1, 3)
        service.write_number("Batch A", 1, 4)
        assert not service.state.busy and service.transport.write_count == 1
        assert "改号" in service.state.message
        service.write_number("Batch A", 1, 3, renumber=True)
        assert len(service.records()) == 1
        assert service.transport.write_count == 1
        service.transport.root, service.transport.point = 15, 16
        service.read_probe()
        wait_for(lambda: not service.state.busy)
        assert service.verified_assignment().point == 3
        assert service.verified_number("Batch A") is None
        service.write_number("Batch A", 15, 16)
        assert len(service.records()) == 1
        assert "改号" in service.state.message
    finally:
        service.close()
        service.close()


def test_lost_response_reopens_pending_without_retrying_write(tmp_path, monkeypatch):
    fast_power_cycle(monkeypatch)
    service = NumberingService(tmp_path, demo=True)
    try:
        connect(service)
        original = service.transport.request

        def lose_write_reply(address, op, payload, token, callback):
            if op == WRITE_NUMBER:
                original(address, op, payload, token, lambda *_: callback(None, FixtureError("测试超时")))
            else:
                original(address, op, payload, token, callback)

        monkeypatch.setattr(service.transport, "request", lose_write_reply)
        service.write_number("Batch A", 2, 254)
        wait_for(lambda: not service.state.busy)
        assert service.state.pending is not None
        assert "测试超时" in service.state.pending.note
        assert service.transport.write_count == 1
    finally:
        service.close()

    restored = NumberingService(tmp_path, demo=True)
    completed = []
    restored.completed.connect(completed.append)
    try:
        connect(restored)
        assert restored.state.pending.point == 254
        verify(restored)
        assert restored.transport.write_count == 0
        assert completed[0].next_point is None and "上限" in completed[0].note
        assert not (tmp_path / "numbering.db").exists()
    finally:
        restored.close()


def test_defer_preserves_batch_slot_and_rom_reservation(tmp_path, monkeypatch):
    fast_power_cycle(monkeypatch)
    service = NumberingService(tmp_path, demo=True)
    try:
        connect(service)
        service.write_number("Batch A", 2, 5)
        wait_for(lambda: not service.state.busy)
        original = service.state.pending
        service.defer_record()
        assert service.state.pending is None
        service.read_probe()
        wait_for(lambda: not service.state.busy)
        service.write_number("Other", 2, 6, renumber=True)
        assert service.transport.write_count == 1
        assert "待核验" in service.state.message
        service.demo_insert(new_probe=True)
        wait_for(lambda: not service.state.busy)
        service.write_number("batch a", 2, 5)
        assert service.transport.write_count == 1
        assert "待核验" in service.state.message
        service.write_number("Batch A", 2, 6)
        wait_for(lambda: not service.state.busy)
        verify(service)
        assert service.state.phase == "verified"
        assert service.records()[1].id == original.id
        assert service.records()[1].status == "pending"
        service.resume_record(original.id)
        service.verify_power_cycle()
        wait_for(lambda: not service.state.busy)
        assert service.state.pending.id == original.id
        assert "原探头" in service.state.message
        assert service.transport.write_count == 2
    finally:
        service.close()


def test_demo_inventory_migrates_and_recovers_deferred_probe_after_restart(tmp_path, monkeypatch):
    fast_power_cycle(monkeypatch)
    inventory_path = tmp_path / "numbering-demo-eeprom.json"
    inventory_path.write_text(json.dumps({"index": 17, "root": 1, "point": 2}), encoding="utf-8")
    body = b"\x2c" + (17).to_bytes(6, "little")
    original_rom = (body + bytes((crc8(body),))).hex().upper()
    service = NumberingService(tmp_path, demo=True)
    try:
        connect(service)
        assert service.state.probe.rom_hex == original_rom
        assert (service.state.probe.root, service.state.probe.point) == (1, 2)
        service.write_number("Batch A", 2, 9)
        wait_for(lambda: not service.state.busy)
        pending_id = service.state.pending.id
        service.defer_record()
        service.demo_insert(new_probe=True)
        wait_for(lambda: not service.state.busy)
        next_rom = service.state.probe.rom_hex
        assert next_rom != original_rom
        service.write_number("Batch A", 2, 10)
        wait_for(lambda: not service.state.busy)
        verify(service)
        assert service.state.phase == "verified"
    finally:
        service.close()

    persisted = json.loads(inventory_path.read_text(encoding="utf-8"))
    assert persisted == {"version": 1, "current_rom": next_rom, "probes": {
        original_rom: {"root": 2, "point": 9}, next_rom: {"root": 2, "point": 10},
    }}

    restored = NumberingService(tmp_path, demo=True)
    completions = []
    restored.completed.connect(completions.append)
    try:
        connect(restored)
        assert restored.state.pending.id == pending_id
        restored.read_probe()
        wait_for(lambda: not restored.state.busy)
        assert restored.state.probe.rom_hex == next_rom
        restored.demo_restore_pending()
        wait_for(lambda: not restored.state.busy)
        assert restored.state.phase == "pending" and not completions
        assert restored.state.probe.rom_hex == original_rom
        assert (restored.state.probe.root, restored.state.probe.point) == (2, 9)
        verify(restored)
        assert [record.id for record in completions] == [pending_id]
        assert restored.transport.write_count == 0
    finally:
        restored.close()


def test_demo_restore_never_uses_expected_values_as_eeprom_contents(tmp_path, monkeypatch):
    fast_power_cycle(monkeypatch)
    service = NumberingService(tmp_path, demo=True)
    try:
        connect(service)
        probe = service.state.probe
        # A pending row with no dispatched write must keep the actual old EEPROM data.
        record = service.storage.begin("Batch A", probe, 2, 20, False, False)
        service.demo_insert(new_probe=True)
        wait_for(lambda: not service.state.busy)
        service.resume_record(record.id)
        service.demo_restore_pending()
        wait_for(lambda: not service.state.busy)
        assert (service.state.probe.root, service.state.probe.point) == (probe.root, probe.point)
        assert service.state.pending.id == record.id
        verify(service)
        assert service.records()[0].status == "failed"
        assert service.transport.write_count == 0
    finally:
        service.close()


def test_same_values_skip_eeprom_and_csv_preserves_audit(tmp_path, monkeypatch):
    fast_power_cycle(monkeypatch)
    service = NumberingService(tmp_path, demo=True)
    try:
        connect(service)
        service.write_number(" =SUM(A1)", 75, 70, advance=False)
        assert service.state.pending.status == "pending"
        verify(service)
        assert service.transport.write_count == 0
        assert service.records()[0].next_point is None
        assert service.records()[0].batch == "=SUM(A1)"
        destination = tmp_path / "records.csv"
        assert service.export_csv(destination) == 1
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            row = next(csv.DictReader(stream))
        assert row["批次"] == "'=SUM(A1)" and row["状态"] == "verified"
    finally:
        service.close()


def test_confirmed_old_value_can_be_marked_failed_after_power_cycle(tmp_path, monkeypatch):
    fast_power_cycle(monkeypatch)
    service = NumberingService(tmp_path, demo=True)
    try:
        connect(service)
        original = service.transport.request

        def reject(address, op, payload, token, callback):
            if op == WRITE_NUMBER:
                callback(None, FixtureError("检测到多只探头，请只接入一只。", 3))
            else:
                original(address, op, payload, token, callback)

        monkeypatch.setattr(service.transport, "request", reject)
        service.write_number("Batch A", 2, 5)
        assert service.state.pending is not None
        assert service.records()[0].status == "pending"
        verify(service)
        assert service.state.pending is None
        assert service.records()[0].status == "failed"
        assert service.verified_assignment() is None
        assert service.transport.write_count == 0
    finally:
        service.close()

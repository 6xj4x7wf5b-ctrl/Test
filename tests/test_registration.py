"""Registration state-machine tests using actual simulator wire responses."""
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from warehouse_pc import simulator
from warehouse_pc.protocol import parse_response, with_crc
from warehouse_pc.registration import RegistrationDialog, validate_backup
from warehouse_pc.simulator import ProtocolSimulator


FIELD_ROMS = tuple(map(bytes.fromhex, ("2CEE08640200EEC9", "2CEE49E18AAC1164")))


class FakeManager(QObject):
    response = Signal(str, object, bytes)
    failure = Signal(str, str)
    request_failed = Signal(str, object, str)

    def __init__(self):
        super().__init__()
        self.simulator = ProtocolSimulator()
        self.requests = []
        self.fail_after_write = None
        self.fail_before_write = None
        self.legacy_result = None
        self.changed_page = None
        self.hold_op = None
        self.held = []
        self.scan_error = False

    def is_simulator(self, _):
        return True

    def submit(self, profile, request, expected, context=None):
        self.requests.append(bytes(request))

        def deliver():
            if self.fail_before_write == request[2]:
                self.fail_before_write = None
                self.request_failed.emit(profile, context, "测试：写请求未执行")
                return
            if self.legacy_result and request[2] == 9:
                raw = with_crc(bytes((request[0], 0x41, 9, self.legacy_result)))
            else:
                raw = self.simulator.simulate_request(request, expected)
            if request[2] == 9 and (request[3], int.from_bytes(request[4:6], "big")) == self.changed_page:
                raw = with_crc(raw[:10] + ((int.from_bytes(raw[10:14], "big") + 1) & 0xFFFFFFFF).to_bytes(4, "big")
                               + raw[14:-2])
            if self.scan_error and request[2] == 0:
                board = self.simulator.board(request[0])
                if board.scan_result == 0 and board.discovered:
                    board.scan_result = 6
                    raw = self.simulator.simulate_request(request, expected)
            if self.fail_after_write == request[2]:
                self.fail_after_write = None
                self.request_failed.emit(profile, context, "测试：写入后响应丢失")
            else:
                self.response.emit(profile, {"context": context, "parsed": parse_response(raw, expected)}, raw)
        if request[2] == self.hold_op:
            self.hold_op = None
            self.held.append(deliver)
        else:
            QTimer.singleShot(0, deliver)


class RegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.warning_patch = patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Ok)
        self.warning = self.warning_patch.start()
        self.open_dialog()

    def open_dialog(self, manager=None, **kwargs):
        if hasattr(self, "dialog"):
            self.dialog._disconnect()
            self.dialog.deleteLater()
        self.manager = manager or FakeManager()
        self.dialog = RegistrationDialog(self.manager, "test-simulator", 1, "模拟分机", **kwargs)
        self.loaded = []
        self.dialog.bindings_loaded.connect(self.loaded.append)
        self.wait_idle()

    def tearDown(self):
        self.dialog._disconnect()
        self.dialog.deleteLater()
        self.application.processEvents()
        self.warning_patch.stop()

    def until(self, predicate, seconds=8):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            self.application.processEvents()
        self.assertTrue(predicate(), self.dialog.status_label.text())

    def wait_idle(self):
        self.until(lambda: self.dialog.info is not None and not self.dialog._busy)

    def test_initial_full_table_and_complete_scan_pagination(self):
        self.assertEqual(len(self.dialog._active), 170)
        self.assertEqual(len(self.loaded[0]["bindings"]), 170)
        self.assertFalse(any(request[2] == 7 for request in self.manager.requests))
        binding_pages = [request for request in self.manager.requests if request[2] == 9]
        self.assertEqual([int.from_bytes(request[4:6], "big") for request in binding_pages], list(range(0, 170, 20)))
        self.assertTrue(all(request[3] == 0 for request in binding_pages))
        self.assertTrue(self.dialog.binding_table.isColumnHidden(4))
        self.assertTrue(self.dialog.scan_bus.isHidden())
        self.assertFalse(self.dialog.dirty)
        field_probes = dict(zip((1001, 1002), FIELD_ROMS))
        generated_rom = simulator._rom
        with patch.object(simulator, "_rom", side_effect=lambda serial: field_probes.get(serial) or generated_rom(serial)):
            self.dialog.start_scan()
            self.wait_idle()
        self.assertEqual(self.dialog.probe_combo.count(), 171)
        for index, rom in enumerate(FIELD_ROMS):
            self.assertEqual(self.dialog.probe_combo.itemText(index + 1), rom.hex(" ").upper())
        pages = [request for request in self.manager.requests if request[2] == 2]
        self.assertEqual([int.from_bytes(request[3:5], "big") for request in pages],
                         [0, 27, 54, 81, 108, 135, 162])
        before = len(self.manager.requests)
        self.dialog.probe_combo.setCurrentIndex(1)
        self.dialog.bind_probe()
        self.assertEqual(len(self.manager.requests), before)
        self.assertEqual(self.dialog.binding_table.item(0, 3).text(), "待修改")
        self.assertNotEqual(self.manager.simulator.board(1).staged[("grain", 1)][1], FIELD_ROMS[0])

    def test_failed_scan_never_exposes_partial_results(self):
        self.manager.scan_error = True
        self.dialog.start_scan()
        self.wait_idle()
        self.assertEqual(self.dialog.probe_combo.count(), 1)
        self.assertFalse(any(request[2] == 2 for request in self.manager.requests))
        self.assertIn("未完整成功", self.dialog.status_label.text())

    def test_save_response_lost_reconciles_without_repeating_write(self):
        self.dialog.binding_table.selectRow(2)
        self.dialog.unbind_probe()
        self.manager.fail_after_write = 5
        events = []
        self.dialog.bindings_saved.connect(events.append)
        self.dialog.save()
        self.wait_idle()
        self.assertEqual(sum(request[2] == 5 for request in self.manager.requests), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["bindings"]), 169)
        self.assertFalse(self.dialog._uncertain)
        self.assertFalse(self.dialog.dirty)
        self.assertIn("只读回查已与目标一致", self.dialog.status_label.text())

    def test_restore_replaces_only_local_draft(self):
        rows = [dict(row, rom=rom) for row, rom in zip(self.dialog._active[:2], FIELD_ROMS)]
        document = self.backup_document(rows)
        before = len(self.manager.requests)
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "backup.json"
            filename.write_text(json.dumps(document), encoding="utf-8")
            with patch("warehouse_pc.registration.QFileDialog.getOpenFileName", return_value=(str(filename), "")), \
                 patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                self.dialog.restore()
        board = self.manager.simulator.board(1)
        self.assertEqual(len(self.manager.requests), before)
        self.assertEqual(self.dialog._draft, rows)
        self.assertEqual(len(board.staged), 170)
        self.assertEqual(len(board.bindings), 170)
        self.assertTrue(self.dialog.dirty)
        self.assertFalse(any(request[2] == 5 for request in self.manager.requests))

    def test_save_writes_only_changes_and_reads_back_swap(self):
        self.assertEqual(self.dialog.summary_label.text(), "已保存 170 条")
        before = self.dialog._active
        draft = [dict(before[0], rom=before[1]["rom"]), dict(before[1], rom=before[0]["rom"]), *before[2:]]
        self.dialog._edit(draft, "交换两个探头")
        self.assertEqual(self.dialog.summary_label.text(), "已保存 170 条 · 未保存 2 项")
        saved = []
        self.dialog.bindings_saved.connect(saved.append)
        self.dialog.save()
        self.wait_idle()
        mutations = [request for request in self.manager.requests if request[2] in (3, 4, 5, 6)]
        self.assertEqual([request[2] for request in mutations], [4, 4, 3, 3, 5])
        self.assertEqual(saved[0]["bindings"], draft)
        self.assertFalse(self.dialog.dirty)
        self.assertEqual(self.dialog.summary_label.text(), "已保存 170 条")
        self.assertEqual(self.dialog.binding_table.item(0, 3).text(), "已保存")

    def test_stage_timeout_uses_readonly_recovery_without_committing(self):
        self.dialog.binding_table.selectRow(2)
        self.dialog.unbind_probe()
        self.manager.fail_after_write = 4
        saved = []
        self.dialog.bindings_saved.connect(saved.append)
        self.dialog.save()
        self.wait_idle()
        self.assertEqual([request[2] for request in self.manager.requests if request[2] in (3, 4, 5, 6)], [4])
        self.assertIn(("grain", 3), self.manager.simulator.board(1).bindings)
        self.assertNotIn(("grain", 3), self.manager.simulator.board(1).staged)
        self.assertEqual(saved, [])
        self.assertTrue(self.dialog.dirty)
        self.assertFalse(self.dialog._uncertain)
        self.assertIn("未自动重发", self.dialog.status_label.text())

    def test_save_timeout_without_commit_is_not_reported_as_success(self):
        self.dialog.binding_table.selectRow(2)
        self.dialog.unbind_probe()
        self.manager.fail_before_write = 5
        saved = []
        self.dialog.bindings_saved.connect(saved.append)
        self.dialog.save()
        self.wait_idle()
        self.assertEqual(sum(request[2] == 5 for request in self.manager.requests), 1)
        self.assertEqual(saved, [])
        self.assertTrue(self.dialog.dirty)
        self.assertIn("未自动重发", self.dialog.status_label.text())

    def test_legacy_empty_fast_path_and_nonempty_fallback(self):
        for result, empty in ((1, True), (2, False)):
            with self.subTest(result=result, empty=empty):
                manager = FakeManager()
                manager.legacy_result = result
                if empty:
                    board = manager.simulator.board(1)
                    board.bindings.clear()
                    board.staged.clear()
                self.open_dialog(manager)
                requests = [request[2] for request in manager.requests]
                self.assertEqual(requests.count(9), 1)
                self.assertEqual(requests.count(7), 0 if empty else 170)
                self.assertEqual(len(self.loaded[0]["bindings"]), 0 if empty else 170)

    def test_changed_revision_does_not_publish_partial_table_or_fallback(self):
        self.manager.changed_page = (0, 20)
        self.manager.requests.clear()
        self.dialog.refresh()
        self.wait_idle()
        self.assertEqual([request[2] for request in self.manager.requests], [0, 9, 9])
        self.assertEqual(len(self.loaded), 1)
        self.assertEqual(len(self.dialog._active), 170)
        self.assertIn("变化", self.dialog.status_label.text())

    def test_embedded_read_can_cancel_but_write_cannot(self):
        parent = QWidget()
        manager = FakeManager()
        self.open_dialog(manager, embedded=True, port_name="COM3", parent=parent)
        self.assertEqual(self.dialog.windowType(), Qt.WindowType.Widget)
        manager.hold_op = 9
        self.dialog.refresh()
        self.until(lambda: bool(manager.held))
        before = len(manager.requests)
        self.dialog.cancel_read()
        manager.held.pop()()
        self.wait_idle()
        self.assertEqual(len(manager.requests), before)
        self.assertIn("已取消读取", self.dialog.status_label.text())
        self.assertEqual(len(self.loaded), 1)

        self.dialog.binding_table.selectRow(2)
        self.dialog.unbind_probe()
        manager.hold_op = 4
        self.dialog.save()
        self.until(lambda: bool(manager.held))
        self.assertFalse(self.dialog.close_button.isEnabled())
        self.dialog.cancel_read()
        self.assertTrue(self.dialog._writing)
        self.assertFalse(self.dialog._cancel_requested)
        manager.held.pop()()
        self.wait_idle()
        self.assertFalse(self.dialog.dirty)
        self.dialog.setParent(None)

    def test_embedded_close_waits_for_pending_read_then_emits_finished(self):
        parent = QWidget()
        self.open_dialog(embedded=True, parent=parent)
        self.manager.hold_op = 9
        self.dialog.refresh()
        self.until(lambda: bool(self.manager.held))
        events = []
        self.dialog.finished.connect(events.append)
        self.dialog.reject()
        self.assertEqual(events, [])
        self.manager.held.pop()()
        self.assertEqual(events, [0])
        self.assertTrue(self.dialog._closed)
        self.dialog.setParent(None)

    def test_external_active_change_prevents_writes_and_keeps_draft(self):
        self.dialog.binding_table.selectRow(2)
        self.dialog.unbind_probe()
        self.manager.simulator.board(1).generation += 1
        self.manager.requests.clear()
        self.dialog.save()
        self.wait_idle()
        self.assertEqual([request[2] for request in self.manager.requests], [0])
        self.assertTrue(self.dialog.dirty)
        self.assertIn("已保存的登记有变化", self.dialog.status_label.text())

    def backup_document(self, rows):
        return {
            "format": "warehouse-registration-v1", "table": "active", "simulated": True,
            "board": {"address": 1, "name": "模拟分机"}, "generation": 1,
            "capacity": {key: self.dialog.info[key] for key in ("grain_capacity", "ambient_capacity", "bus_count")},
            "binding_count": len(rows),
            "bindings": [{**row, "rom": row["rom"].hex()} for row in rows],
        }

    def test_backup_validates_address_duplicates_capacity_and_completeness(self):
        document = self.backup_document(self.dialog._active[:2])
        self.assertEqual(len(validate_backup(document, 1, self.dialog.info)), 2)
        for change in ("address", "duplicate", "capacity", "incomplete"):
            bad = copy.deepcopy(document)
            if change == "address":
                bad["board"]["address"] = 2
            elif change == "duplicate":
                bad["bindings"][1]["rom"] = bad["bindings"][0]["rom"]
            elif change == "capacity":
                bad["bindings"][0]["point"] = 999
            else:
                bad["binding_count"] = 3
            with self.assertRaises(ValueError, msg=change):
                validate_backup(bad, 1, self.dialog.info)

    def test_dirty_close_defaults_to_cancel(self):
        self.dialog.binding_table.selectRow(2)
        self.dialog.unbind_probe()
        closed = []
        self.dialog.finished.connect(closed.append)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question:
            self.dialog.reject()
        question.assert_called_once()
        self.assertEqual(closed, [])
        self.assertTrue(self.dialog.dirty)


if __name__ == "__main__":
    unittest.main()

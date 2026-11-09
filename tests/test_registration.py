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

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from warehouse_pc.protocol import parse_response
from warehouse_pc.registration import RegistrationDialog, validate_backup
from warehouse_pc.simulator import ProtocolSimulator


class FakeManager(QObject):
    response = Signal(str, object, bytes)
    failure = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.simulator = ProtocolSimulator()
        self.requests = []
        self.fail_after_write = None
        self.scan_error = False

    def is_simulator(self, _):
        return True

    def submit(self, profile, request, expected, context=None):
        self.requests.append(bytes(request))

        def deliver():
            raw = self.simulator.simulate_request(request, expected)
            if self.scan_error and request[2] == 0:
                board = self.simulator.board(request[0])
                if board.scan_result == 0 and board.discovered:
                    board.scan_result = 6
                    raw = self.simulator.simulate_request(request, expected)
            if self.fail_after_write == request[2]:
                self.fail_after_write = None
                self.failure.emit(profile, "测试：写入后响应丢失")
            else:
                self.response.emit(profile, {"context": context, "parsed": parse_response(raw, expected)}, raw)
        QTimer.singleShot(0, deliver)


class RegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.warning_patch = patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Ok)
        self.warning = self.warning_patch.start()
        self.manager = FakeManager()
        self.dialog = RegistrationDialog(self.manager, "test-simulator", 1, "模拟分机")
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
        self.assertEqual(len(self.dialog._cache[1]), 170)
        self.assertFalse(self.dialog.dirty)
        self.dialog.start_scan()
        self.wait_idle()
        self.assertEqual(self.dialog.scan_table.rowCount(), 170)
        pages = [request for request in self.manager.requests if request[2] == 2]
        self.assertEqual([int.from_bytes(request[3:5], "big") for request in pages],
                         [0, 27, 54, 81, 108, 135, 162])

    def test_failed_scan_never_exposes_partial_results(self):
        self.manager.scan_error = True
        self.dialog.start_scan()
        self.wait_idle()
        self.assertEqual(self.dialog.scan_table.rowCount(), 0)
        self.assertFalse(any(request[2] == 2 for request in self.manager.requests))
        self.assertIn("未完整成功", self.dialog.status_label.text())

    def test_save_response_lost_reconciles_without_repeating_write(self):
        board = self.manager.simulator.board(1)
        del board.staged[("grain", 3)]
        board.dirty = True
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

    def test_restore_replaces_staged_not_union_and_does_not_save(self):
        self.dialog.table_combo.setCurrentIndex(1)
        self.dialog.read_bindings()
        self.wait_idle()
        rows = self.dialog._cache[0][:2]
        document = self.backup_document(rows)
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "backup.json"
            filename.write_text(json.dumps(document), encoding="utf-8")
            with patch("warehouse_pc.registration.QFileDialog.getOpenFileName", return_value=(str(filename), "")), \
                 patch("warehouse_pc.registration.QInputDialog.getText", return_value=("恢复地址1", True)):
                self.dialog.restore()
                self.wait_idle()
        board = self.manager.simulator.board(1)
        self.assertEqual(len(board.staged), 2)
        self.assertEqual(len(board.bindings), 170)
        self.assertTrue(self.dialog.dirty)
        self.assertFalse(any(request[2] == 5 for request in self.manager.requests))

    def backup_document(self, rows):
        return {
            "format": "warehouse-registration-v1", "table": "active", "simulated": True,
            "board": {"address": 1, "name": "模拟分机"}, "generation": 1,
            "capacity": {key: self.dialog.info[key] for key in ("grain_capacity", "ambient_capacity", "bus_count")},
            "binding_count": len(rows),
            "bindings": [{**row, "rom": row["rom"].hex()} for row in rows],
        }

    def test_backup_validates_address_duplicates_capacity_and_completeness(self):
        document = self.backup_document(self.dialog._cache[1][:2])
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
        self.dialog.dirty = True
        closed = []
        self.dialog.finished.connect(closed.append)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question:
            self.dialog.reject()
        question.assert_called_once()
        self.assertEqual(closed, [])
        self.assertTrue(self.dialog.dirty)


if __name__ == "__main__":
    unittest.main()

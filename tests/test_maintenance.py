"""Real Repository/Store integration for destructive maintenance boundaries."""
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDateTime, QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from warehouse_pc.maintenance_ui import MaintenancePage, configuration_source
from warehouse_pc.repository import Repository


class RecordingRepository(Repository):
    def __init__(self, path):
        super().__init__(path)
        self.calls = []

    def call(self, method, *args, callback=None, on_error=None, **kwargs):
        self.calls.append((method, args, kwargs))
        return super().call(method, *args, callback=callback, on_error=on_error, **kwargs)


class MaintenanceController(QObject):
    """Only serial/UI orchestration is stubbed; SQLite stays on its real worker."""
    log = Signal(str)
    metadata_changed = Signal(object)

    def __init__(self, repository, metadata):
        super().__init__()
        self.repository, self.metadata = repository, metadata
        self.demo = False
        self.running = True
        self.disconnects = 0
        self.alarms_refreshed = 0
        self.latest = {1: "old sample"}
        self.last_stored = {1: 1}
        self.last_received = {1: 1}
        self.cycles = {"old": {}}
        self.processing_devices = {1}

    def set_running(self, value):
        self.running = value

    def disconnect_all(self):
        self.running = False
        self.disconnects += 1
        self.cycles.clear()

    def set_intervals(self, poll, history):
        self.intervals = (poll, history)

    def reload_metadata(self, auto_start=False, callback=None, on_error=None):
        def done(metadata):
            self.metadata = metadata
            self.metadata_changed.emit(metadata)
            if callback:
                callback(metadata)
        self.repository.call("metadata", callback=done, on_error=on_error)

    def refresh_alarms(self):
        self.alarms_refreshed += 1


class MaintenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.repository = RecordingRepository(self.root / "live.db")
        self.errors, ready = [], []
        self.repository.error.connect(self.errors.append)
        self.repository.ready.connect(lambda: ready.append(True))
        self.repository.start()
        self.until(lambda: bool(ready or self.errors))
        self.assertFalse(self.errors)
        self.warehouse = self.call("save_warehouse", "一号仓")
        self.port = self.call("save_port", "串口一", "COM1")
        self.device = self.call("save_device", "分机一", self.warehouse, self.port, 1, "LC-M3", 10, 1)
        self.call("save_calibration", self.device, "grain", 1, 0.5, 0)
        self.call("save_threshold", self.device, "grain", temp_high=30)
        self.call("save_samples", self.device, [{"kind": "grain", "point": 1, "temperature": 21, "status": 0}],
                  timestamp="2026-10-15T10:00:00", source="serial")
        self.controller = MaintenanceController(self.repository, self.call("metadata"))
        self.page = MaintenancePage(self.repository, self.controller, self.root)
        self.page.start_date.setDateTime(QDateTime.fromString("2026-10-01T00:00:00", "yyyy-MM-ddTHH:mm:ss"))
        self.page.end_date.setDateTime(QDateTime.fromString("2026-10-31T23:59:59", "yyyy-MM-ddTHH:mm:ss"))
        self.warning_patch = patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Ok)
        self.warning = self.warning_patch.start()

    def tearDown(self):
        if hasattr(self, "page"):
            self.page._space_timer.stop()
            self.page.deleteLater()
        self.application.processEvents()
        self.assertTrue(self.repository.shutdown())
        self.warning_patch.stop()
        self.directory.cleanup()

    def until(self, predicate, seconds=8):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            self.application.processEvents()
        self.assertTrue(predicate(), "等待 Qt/Repository 操作超时")

    def call(self, method, *args, **kwargs):
        result, failures = [], []
        self.repository.call(method, *args, callback=result.append, on_error=failures.append, **kwargs)
        self.until(lambda: bool(result or failures))
        self.assertEqual(failures, [])
        return result[0]

    def idle(self):
        self.until(lambda: not self.page._busy)
        self.assertEqual(self.errors, [])

    def test_cleanup_cancel_only_counts_and_keeps_samples(self):
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.page.clear_history()
            self.idle()
        self.assertEqual(self.call("history_count"), 1)
        calls = [kwargs["dry_run"] for method, _, kwargs in self.repository.calls if method == "clear_history"]
        self.assertEqual(calls, [True])
        self.assertTrue(self.controller.running)

    def test_cleanup_confirm_deletes_samples_and_preserves_configuration(self):
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.page.clear_history()
            self.idle()
        self.assertEqual(self.call("history_count"), 0)
        self.assertEqual(len(self.call("devices")), 1)
        self.assertEqual(len(self.call("warehouses")), 1)
        self.assertEqual(len(self.call("calibrations", self.device)), 1)
        self.assertEqual(len(self.call("thresholds", self.device)), 1)
        calls = [kwargs["dry_run"] for method, _, kwargs in self.repository.calls if method == "clear_history"]
        self.assertEqual(calls, [True, False])

    def test_configuration_rejects_cross_source_before_import(self):
        manifest = self.call("export_config")
        for name in ("SIMULATOR", "SIM", "sim://bus-a", "simulator://bus-b"):
            bad = copy.deepcopy(manifest)
            bad["ports"][0]["port_name"] = name
            with self.assertRaises(ValueError, msg=name):
                configuration_source(bad, "serial")
        with self.assertRaises(ValueError):
            configuration_source(manifest, "simulator")
        bad = copy.deepcopy(manifest)
        bad["source"], bad["demo"] = "simulator", True
        filename = self.root / "demo.json"
        filename.write_text(json.dumps(bad), encoding="utf-8")
        with patch("warehouse_pc.maintenance_ui.QFileDialog.getOpenFileName", return_value=(str(filename), "")):
            self.page.import_configuration()
        self.assertFalse(any(method == "import_config" for method, _, _ in self.repository.calls))
        self.warning.assert_called_once()
        self.assertEqual(self.call("history_count"), 1)

    def test_full_restore_saves_safety_before_replacing_and_holds_global_busy(self):
        snapshot = self.root / "snapshot.db"
        self.call("backup", snapshot)
        self.call("save_warehouse", "恢复后应移除的仓房")
        self.call("set_setting", "collection", {"poll_seconds": 99, "history_seconds": 99})
        busy, metadata_during_busy = [], []
        self.page.busy_changed.connect(busy.append)
        self.controller.metadata_changed.connect(lambda _: metadata_during_busy.append(self.page._busy))
        with patch("warehouse_pc.maintenance_ui.QFileDialog.getOpenFileName", return_value=(str(snapshot), "")), \
             patch("warehouse_pc.maintenance_ui.QInputDialog.getText", return_value=("恢复全部数据库", True)):
            self.page.restore_database()
            self.assertTrue(self.page._busy)
            self.assertFalse(self.controller.running)
            self.assertEqual(self.controller.disconnects, 1)
            self.idle()
        self.assertEqual(busy, [True, False])
        self.assertEqual(metadata_during_busy, [True])
        safety_calls = [(index, args[0]) for index, (method, args, _) in enumerate(self.repository.calls)
                        if method == "backup" and Path(args[0]).parent.name == "safety"]
        restore_calls = [(index, kwargs) for index, (method, _, kwargs) in enumerate(self.repository.calls) if method == "restore_backup"]
        self.assertEqual(len(safety_calls), 1)
        self.assertEqual(len(restore_calls), 1)
        self.assertLess(safety_calls[0][0], restore_calls[0][0])
        self.assertTrue(Path(safety_calls[0][1]).is_file())
        self.assertEqual(restore_calls[0][1]["expected_source"], "serial")
        self.assertEqual(len(self.call("warehouses")), 1)
        self.assertEqual(self.call("history_count"), 1)
        self.assertEqual(self.controller.intervals, (5, 30))
        for name in ("latest", "last_stored", "last_received", "cycles", "processing_devices"):
            self.assertFalse(getattr(self.controller, name), name)
        self.assertFalse(self.controller.running)
        self.assertIn("数据库恢复完成", self.page.status_label.text())

    def test_log_display_is_bounded(self):
        for index in range(1100):
            self.controller.log.emit(str(index))
        self.assertLessEqual(self.page.log_view.document().blockCount(), 1000)
        self.assertIn("1099", self.page.log_view.toPlainText())
        self.assertNotIn("  0\n", self.page.log_view.toPlainText())


if __name__ == "__main__":
    unittest.main()

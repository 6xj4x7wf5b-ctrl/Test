from __future__ import annotations

import copy
import csv
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from warehouse_pc.domain import export_csv, export_excel, rom_hex
from warehouse_pc.storage import Store


def valid_rom(serial=1):
    data = bytearray([0x28, serial, 0, 0, 0, 0, 0, 0])
    crc = 0
    for byte in data[:7]:
        for _ in range(8):
            mix = (crc ^ byte) & 1
            crc >>= 1
            if mix:
                crc ^= 0x8C
            byte >>= 1
    data[-1] = crc
    return data.hex().upper()


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "nested" / "test.sqlite3"
        self.store = Store(self.path)
        self.warehouse = self.store.save_warehouse("1号仓")
        self.port = self.store.save_port("串口连接", "COM7", parity="E", stop_bits=2)
        self.device = self.store.save_device("测温分机", self.warehouse, self.port, grain_count=10, ambient_count=2)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def grain(self, temp=20.0, point=1, status="ok"):
        return {"kind": "grain", "point": point, "temperature": temp, "status": status}

    def test_metadata_empty_data_and_validation(self):
        self.assertEqual(self.store.history(), [])
        self.assertEqual(self.store.alarms(), [])
        row = self.store.devices()[0]
        self.assertEqual((row["warehouse_name"], row["connection_name"], row["source"]), ("1号仓", "串口连接", "serial"))
        self.assertEqual((row["parity"], row["stop_bits"]), ("E", 2))
        for kwargs in ({"address": 0}, {"address": 248}, {"grain_count": 166}, {"ambient_count": 6}, {"protocol": "unknown"}):
            with self.assertRaises(ValueError):
                self.store.save_device("无效", self.warehouse, self.port, **kwargs)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_device("重复地址", self.warehouse, self.port)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.delete_port(self.port)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.delete_warehouse(self.warehouse)

    def test_correction_is_once_and_historical(self):
        self.store.save_calibration(self.device, "grain", 1, temp_offset=1.5)
        preview = self.store.preview_samples(self.device, [self.grain()], "2026-10-15T10:00:00")
        self.assertEqual(self.store.history_count(), 0)
        self.assertEqual(preview[0]["raw_temp"], 20)
        self.assertEqual(preview[0]["temp"], 21.5)
        saved = self.store.save_samples(self.device, preview, "2026-10-15T10:00:00")
        self.assertEqual(saved[0]["temp"], 21.5)
        self.assertEqual(saved[0]["calibration_version"], 1)
        self.store.save_calibration(self.device, "grain", 1, temp_offset=4)
        self.assertEqual(self.store.history()[0]["temp"], 21.5)
        self.assertEqual(self.store.history()[0]["temp_offset"], 1.5)
        self.assertEqual(self.store.preview_samples(self.device, [self.grain()])[0]["temp"], 24)
        with self.assertRaises(ValueError):
            self.store.save_samples(self.device, [{"kind": "grain", "point": 1, "temp": 21.5, "calibration_version": 1, "status": "ok"}])

    def test_bad_samples_and_batch_transaction(self):
        for status in (1, 2, 3, 4, 5, 6, 7, 8, 9, "unknown"):
            row = self.store.preview_samples(self.device, [self.grain(20, status=status)])[0]
            self.assertNotEqual(row["status"], "ok")
            self.assertIsNone(row["temp"])
            self.assertEqual(row["raw_temp"], 20)
        invalid = self.store.preview_samples(self.device, [self.grain(999)])[0]
        self.assertEqual(invalid["status"], "data_error")
        self.assertIsNone(invalid["temp"])
        with self.assertRaises(ValueError):
            self.store.save_samples(self.device, [self.grain(), self.grain(point=100)])
        self.assertEqual(self.store.history_count(), 0)
        with self.assertRaises(ValueError):
            self.store.save_samples(self.device, [self.grain(), self.grain()])
        self.assertEqual(self.store.history_count(), 0)
        with self.assertRaises(ValueError):
            self.store.save_calibration(self.device, "grain", 1, float("nan"))
        self.store.save_calibration(self.device, "ambient", 1, humidity_offset=10)
        invalid = self.store.preview_samples(self.device, [{"kind": "ambient", "point": 1, "temp": 20, "humidity": 99, "status": 0}])[0]
        self.assertEqual(invalid["status"], "data_error")
        self.assertEqual(invalid["raw_humidity"], 99)
        self.assertIsNone(invalid["humidity"])

    def test_latest_is_per_point_and_timestamp(self):
        self.store.save_samples(self.device, [self.grain(10), self.grain(11, point=2)], "2026-10-15T10:00:00")
        self.store.save_samples(self.device, [self.grain(20)], "2026-10-15T10:01:00")
        self.store.save_samples(self.device, [self.grain(99, point=2)], "2026-10-15T09:00:00")
        rows = {row["point"]: row for row in self.store.latest_samples(self.device)}
        self.assertEqual(rows[1]["temp"], 20)
        self.assertEqual(rows[2]["temp"], 11)
        self.assertEqual(self.store.history_count(self.device, "2026-10-15T10:00:00", "2026-10-15T10:00:00"), 2)

    def test_alarm_dedup_recovery_ack_and_bad_status(self):
        self.store.save_threshold(self.device, "grain", temp_low=0, temp_high=25)
        rows = self.store.preview_samples(self.device, [self.grain(30)])
        alarm = self.store.evaluate_alarms(self.device, rows)
        self.assertEqual(len(alarm), 1)
        self.assertEqual(self.store.evaluate_alarms(self.device, rows), [])
        self.store.acknowledge_alarm(alarm[0]["id"])
        self.assertIsNotNone(self.store.alarms()[0]["acknowledged_at"])
        self.assertEqual(self.store.alarms()[0]["active"], 1)
        for status in ("stale", "unknown", "offline", "not_ready"):
            self.assertEqual(self.store.evaluate_alarms(self.device, self.store.preview_samples(self.device, [self.grain(20, status=status)])), [])
            self.assertEqual(self.store.alarms()[0]["active"], 1)
        self.store.evaluate_alarms(self.device, self.store.preview_samples(self.device, [self.grain(20)]))
        self.assertEqual(self.store.alarms()[0]["active"], 0)
        new = self.store.evaluate_alarms(self.device, rows)
        self.assertEqual(len(new), 1)
        self.assertNotEqual(new[0]["id"], alarm[0]["id"])
        self.assertIsNone(new[0]["acknowledged_at"])

    def test_source_is_fixed_and_simulator_can_alarm(self):
        port = self.store.save_port("模拟连接", "SIMULATOR")
        device = self.store.save_device("模拟设备", self.warehouse, port)
        self.store.save_threshold(device, "grain", temp_high=25)
        rows = self.store.save_samples(device, [self.grain(30)], source="simulator")
        self.assertEqual(rows[0]["source"], "simulator")
        self.assertEqual(self.store.evaluate_alarms(device, rows)[0]["source"], "simulator")
        with self.assertRaises(ValueError):
            self.store.save_samples(device, [self.grain()], source="serial")
        with self.assertRaises(ValueError):
            self.store.save_samples(self.device, [self.grain()], source="simulator")
        with self.assertRaises(ValueError):
            self.store.save_port("换成模拟", "SIM://example", id=self.port)
        with self.assertRaises(ValueError):
            self.store.save_device("换成模拟", self.warehouse, port, id=self.device)

    def test_clear_preview_validation_and_preserve_alarm(self):
        self.store.save_threshold(self.device, "grain", temp_high=25)
        rows = self.store.save_samples(self.device, [self.grain(30)], "2026-10-15T10:00:00")
        self.store.evaluate_alarms(self.device, rows)
        self.store.save_samples(self.device, [self.grain(20)], "2026-10-15T11:00:00")
        self.assertEqual(self.store.clear_history(end="2026-10-15T10:00:00", dry_run=True), 1)
        self.assertEqual(self.store.history_count(), 2)
        with self.assertRaises(ValueError):
            self.store.clear_history(start="2026-10-16T00:00:00", end="2026-10-15T00:00:00")
        with self.assertRaises(ValueError):
            self.store.clear_history(end="bad date")
        self.assertEqual(self.store.history_count(), 2)
        self.assertEqual(self.store.clear_history(end="2026-10-15T10:00:00"), 1)
        self.assertEqual(self.store.history_count(), 1)
        self.assertEqual(len(self.store.alarms()), 1)
        self.assertIsNone(self.store.alarms()[0]["sample_id"])
        with self.assertRaises(ValueError):
            self.store.delete_device(self.device)

    def test_binding_validation_and_version_snapshots(self):
        table = [{"kind": "grain", "point": 1, "bus": 0, "rom": valid_rom(), "position": "第1层"}]
        self.assertEqual(self.store.set_bindings(self.device, table), 1)
        self.assertEqual(self.store.set_bindings(self.device, table), 1)
        rows = self.store.save_samples(self.device, [self.grain()])
        self.assertEqual(rows[0]["binding_version"], 1)
        table[0]["rom"] = valid_rom(2)
        self.assertEqual(self.store.set_bindings(self.device, table), 2)
        self.assertEqual(self.store.history()[0]["binding_version"], 1)
        table.append({"kind": "grain", "point": 2, "rom": valid_rom(2)})
        with self.assertRaises(ValueError):
            self.store.set_bindings(self.device, table)
        self.assertEqual(self.store.get_binding_version(self.device), 2)
        with self.assertRaises(ValueError):
            rom_hex("2800000000000001")

    def test_backup_is_consistent_and_reopens(self):
        self.store.save_samples(self.device, [self.grain()])
        self.store.set_setting("采集周期", 5)
        path = Path(self.directory.name) / "backups" / "snapshot.sqlite3"
        self.store.backup(path)
        copy_store = Store(path)
        try:
            self.assertEqual(copy_store.history_count(), 1)
            self.assertEqual(copy_store.get_setting("采集周期"), 5)
            self.assertEqual(copy_store._db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            copy_store.close()
        self.store.save_samples(self.device, [self.grain()])
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 1)
        with self.assertRaises(ValueError):
            self.store.backup(self.path)

    def test_config_roundtrip_and_atomic_failure(self):
        self.store.save_calibration(self.device, "grain", 1, 1)
        self.store.save_threshold(self.device, "grain", temp_high=30)
        self.store.set_bindings(self.device, [{"kind": "grain", "point": 1, "rom": valid_rom()}])
        self.store.set_setting("history_interval", 30)
        self.store.save_samples(self.device, [self.grain()])
        exported = self.store.export_config()
        self.assertNotIn("samples", exported)
        self.assertNotIn("alarms", exported)
        self.store.import_config(exported)
        self.assertEqual(self.store.history_count(), 1)
        self.assertEqual(len(self.store.devices()), 1)
        self.assertEqual(self.store.get_setting("history_interval"), 30)
        before = self.store.export_config()
        corrupt = copy.deepcopy(exported)
        corrupt["warehouses"][0]["name"] = "先写入但必须回滚的仓房"
        corrupt["calibrations"][0]["temp_offset"] = float("nan")
        with self.assertRaises(ValueError):
            self.store.import_config(corrupt)
        self.assertEqual(self.store.export_config(), before)
        self.assertEqual(self.store.history_count(), 1)
        path = Path(self.directory.name) / "new.sqlite3"
        imported = Store(path)
        try:
            imported.import_config(exported)
            self.assertEqual(len(imported.devices()), 1)
            self.assertEqual(imported.history_count(), 0)
            self.assertEqual(imported.calibrations(imported.devices()[0]["id"])[0]["temp_offset"], 1)
        finally:
            imported.close()

    def test_csv_and_optional_excel_export(self):
        path = Path(self.directory.name) / "export" / "历史.csv"
        rows = [{"timestamp": "2026-10-15T10:00:00", "source": "simulator", "device_name": "=SUM(A1)", "temp": -1.2}]
        export_csv(path, rows)
        with path.open(encoding="utf-8-sig", newline="") as stream:
            data = list(csv.DictReader(stream))
        self.assertEqual(data[0]["temp"], "-1.2")
        self.assertEqual(data[0]["source"], "simulator")
        self.assertEqual(data[0]["device_name"], "'=SUM(A1)")
        try:
            import openpyxl
        except ImportError:
            return
        xlsx = path.with_suffix(".xlsx")
        export_excel(xlsx, rows)
        book = openpyxl.load_workbook(xlsx, read_only=True)
        try:
            data = list(book.active.values)
            self.assertEqual(data[1][data[0].index("temp")], -1.2)
        finally:
            book.close()

    def test_bulk_calibration_is_one_version_and_atomic(self):
        version = self.store.save_calibrations(self.device, [
            {"kind": "grain", "point": 1, "temp_offset": 1},
            {"kind": "grain", "point": 2, "temp_offset": 2}])
        self.assertEqual(version, 1)
        self.assertEqual({row["version"] for row in self.store.calibrations(self.device)}, {1})
        before = self.store.calibrations(self.device)
        with self.assertRaises(ValueError):
            self.store.save_calibrations(self.device, [
                {"kind": "grain", "point": 1, "temp_offset": 9},
                {"kind": "grain", "point": 100, "temp_offset": 9}])
        self.assertEqual(self.store.calibrations(self.device), before)

    def test_paged_filtered_history_and_full_export(self):
        for index in range(12):
            self.store.save_samples(self.device, [self.grain(20 + index / 10)], f"2026-10-15T10:00:{index:02d}")
        self.assertEqual(self.store.history_count_filters(self.device, kind="grain", point=1), 12)
        page = self.store.history_page(self.device, kind="grain", point=1, limit=3, offset=3)
        self.assertEqual(len(page), 3)
        self.assertEqual(page[0]["timestamp"], "2026-10-15T10:00:08")
        destination = Path(self.directory.name) / "history.csv"
        self.assertEqual(self.store.export_history(destination, self.device, kind="grain", point=1), 12)
        with destination.open(encoding="utf-8-sig") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 12)
        self.assertEqual(self.store.export_history(destination.with_suffix(".xlsx"), self.device, point=1), 12)

    def test_restore_valid_snapshot_and_reject_unsafe_files(self):
        self.store.save_samples(self.device, [self.grain(21)])
        snapshot = Path(self.directory.name) / "restore.sqlite3"
        self.store.backup(snapshot)
        self.store.save_samples(self.device, [self.grain(22)])
        self.store.restore_backup(snapshot, expected_source="serial")
        self.assertEqual(self.store.history_count(), 1)
        self.assertEqual(self.store.history()[0]["temp"], 21)
        with self.assertRaises(ValueError):
            self.store.restore_backup(self.path)
        with self.assertRaises(ValueError):
            self.store.restore_backup(snapshot, expected_source="simulator")
        self.assertEqual(self.store.history_count(), 1)
        invalid = Path(self.directory.name) / "bad.sqlite3"
        invalid.write_bytes(b"not a database")
        with self.assertRaises(ValueError):
            self.store.restore_backup(invalid)
        self.assertEqual(self.store.history_count(), 1)
        wrong_schema = Path(self.directory.name) / "schema.sqlite3"
        self.store.backup(wrong_schema)
        with closing(sqlite3.connect(wrong_schema)) as db, db:
            db.execute("DROP TABLE settings")
        with self.assertRaises(ValueError):
            self.store.restore_backup(wrong_schema)
        future = Path(self.directory.name) / "future.sqlite3"
        self.store.backup(future)
        with closing(sqlite3.connect(future)) as db, db:
            db.execute("PRAGMA user_version=999")
        with self.assertRaises(ValueError):
            self.store.restore_backup(future)
        broken_fk = Path(self.directory.name) / "broken-fk.sqlite3"
        self.store.backup(broken_fk)
        with closing(sqlite3.connect(broken_fk)) as db, db:
            db.execute("UPDATE samples SET device_id=999")
        with self.assertRaises(ValueError):
            self.store.restore_backup(broken_fk)
        mixed_source = Path(self.directory.name) / "source-conflict.sqlite3"
        self.store.backup(mixed_source)
        with closing(sqlite3.connect(mixed_source)) as db, db:
            db.execute("UPDATE samples SET source='simulator'")
        with self.assertRaises(ValueError):
            self.store.restore_backup(mixed_source)
        self.assertEqual(self.store.history_count(), 1)
        self.store.save_samples(self.device, [self.grain(23)])
        self.assertEqual(self.store.history_count(), 2)

    def test_bulk_ack_only_active_unacknowledged(self):
        self.store.save_threshold(self.device, "grain", temp_high=25)
        first = self.store.evaluate_alarms(self.device, self.store.preview_samples(self.device, [self.grain(30)]))[0]
        self.store.evaluate_alarms(self.device, self.store.preview_samples(self.device, [self.grain(20)]))
        second = self.store.evaluate_alarms(self.device, self.store.preview_samples(self.device, [self.grain(30)]))[0]
        self.assertEqual(self.store.acknowledge_alarms(), 1)
        self.assertEqual(self.store.acknowledge_alarms(), 0)
        records = {row["id"]: row for row in self.store.alarms()}
        self.assertIsNone(records[first["id"]]["acknowledged_at"])
        self.assertIsNotNone(records[second["id"]]["acknowledged_at"])

    def test_unacknowledged_active_alarm_survives_display_limit(self):
        self.store.save_threshold(self.device, "grain", temp_high=25)
        pending = self.store.evaluate_alarms(self.device, self.store.preview_samples(
            self.device, [self.grain(30)], timestamp="2020-01-01T00:00:00"))[0]
        acknowledged = self.store.evaluate_alarms(self.device, self.store.preview_samples(
            self.device, [self.grain(30, point=2)], timestamp="2021-01-01T00:00:00"))[0]
        self.store.acknowledge_alarm(acknowledged["id"])
        with self.store._db:
            self.store._db.executemany("""INSERT INTO alarms(device_id,kind,point,metric,direction,value,
                threshold,timestamp,source,active,cleared_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                [(self.device, "grain", 3, "temp", "high", 30, 25, "2030-01-01T00:00:00",
                  "serial", 0, "2030-01-01T00:01:00") for _ in range(501)])
        records = self.store.alarms(limit=500)
        self.assertEqual(len(records), 500)
        self.assertEqual(records[0]["id"], pending["id"])
        self.assertEqual(records[1]["id"], acknowledged["id"])
        self.assertEqual(self.store.alarms(limit=1)[0]["id"], pending["id"])

    def test_export_failure_preserves_existing_report_and_cleans_temp(self):
        self.store.save_samples(self.device, [self.grain()])
        for extension in ("csv", "xlsx"):
            with self.subTest(extension=extension):
                destination = Path(self.directory.name) / f"existing.{extension}"
                destination.write_bytes(b"existing complete report")
                def fail_export(path, *args, **kwargs):
                    Path(path).write_bytes(b"truncated new report")
                    raise OSError("simulated write failure")
                target = "warehouse_pc.domain.export_csv" if extension == "csv" else "openpyxl.workbook.workbook.Workbook.save"
                with patch(target, side_effect=fail_export):
                    with self.assertRaises(OSError):
                        self.store.export_history(destination)
                self.assertEqual(destination.read_bytes(), b"existing complete report")
                self.assertEqual(list(destination.parent.glob(".warehouse-report-*")), [])
                self.assertEqual(self.store.history_count(), 1)
        destination = Path(self.directory.name) / "replace-failed.csv"
        destination.write_bytes(b"old report")
        with patch("warehouse_pc.storage.os.replace", side_effect=OSError("rename failed")):
            with self.assertRaises(OSError):
                self.store.export_history(destination)
        self.assertEqual(destination.read_bytes(), b"old report")
        self.assertEqual(list(destination.parent.glob(".warehouse-report-*")), [])

    def test_import_layouts_remaps_device_ids_and_merges_positions(self):
        original_position = {"line": "原设备线", "layer": "1", "position": "原位置"}
        self.store.set_setting("point_layouts", {str(self.device): {"grain:1": original_position}})
        source = Store(Path(self.directory.name) / "layout-source.sqlite3")
        try:
            warehouse = source.save_warehouse("导入仓房")
            port = source.save_port("导入串口", "COM8")
            device = source.save_device("导入分机", warehouse, port, grain_count=3, ambient_count=1)
            source.set_setting("point_layouts", {str(device): {
                "grain:1": {"line": "新线", "layer": "2", "position": "新位置"},
                "ambient:1": {"line": "仓内", "layer": "", "position": "入口"}}})
            imported = source.export_config()
        finally:
            source.close()
        self.store.import_config(imported)
        target = next(row for row in self.store.devices() if row["name"] == "导入分机")
        self.assertNotEqual(target["id"], device)
        layouts = self.store.get_setting("point_layouts")
        self.assertEqual(layouts[str(self.device)]["grain:1"], original_position)
        self.assertEqual(layouts[str(target["id"])]["grain:1"]["position"], "新位置")
        self.assertEqual(layouts[str(target["id"])]["ambient:1"]["line"], "仓内")
        layouts[str(target["id"])]["grain:2"] = {"line": "本地保留", "layer": "3", "position": "未导入点"}
        self.store.set_setting("point_layouts", layouts)
        self.store.import_config(imported)
        self.assertEqual(self.store.get_setting("point_layouts")[str(target["id"])]["grain:2"]["line"], "本地保留")
        invalid_layouts = [
            [], {"999": {}}, {str(device): []}, {"01": {}},
            {str(device): {"unknown:1": {}}}, {str(device): {"grain:0": {}}},
            {str(device): {"grain:4": {}}}, {str(device): {"ambient:2": {}}},
            {str(device): {"grain:01": {}}}, {str(device): {"grain:1": []}},
            {str(device): {"grain:1": {"line": 1}}},
            {str(device): {"grain:1": {"layer": None}}},
            {str(device): {"grain:1": {"position": ["invalid"]}}},
        ]
        before = self.store.export_config()
        for bad_layout in invalid_layouts:
            with self.subTest(layout=bad_layout):
                bad = copy.deepcopy(imported)
                bad["warehouses"][0]["name"] = "导入失败必须回滚的仓房"
                bad["settings"]["point_layouts"] = bad_layout
                with self.assertRaises(ValueError):
                    self.store.import_config(bad)
                self.assertEqual(self.store.export_config(), before)

    def test_deleted_device_id_reuse_does_not_inherit_layout(self):
        position = {"grain:1": {"line": "旧线", "layer": "1", "position": "旧设备位置"}}
        unrelated = {"grain:1": {"line": "保留", "layer": "2", "position": "其他设备位置"}}
        self.store.set_setting("point_layouts", {str(self.device): position, "999": unrelated})
        self.store.delete_device(self.device)
        replacement = self.store.save_device("替换设备", self.warehouse, self.port)
        self.assertEqual(replacement, self.device)
        layouts = self.store.get_setting("point_layouts")
        self.assertNotIn(str(replacement), layouts)
        self.assertEqual(layouts["999"], unrelated)


if __name__ == "__main__":
    unittest.main()

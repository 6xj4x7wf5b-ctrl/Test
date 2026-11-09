"""Local SQLite data and configuration storage, owned by the DB worker thread."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import suppress
from pathlib import Path
from urllib.parse import quote

from .domain import (
    finite_number, iso_timestamp, normalize_kind, normalize_source, normalize_status,
    positive_int, rom_hex, source_for_port, time_range,
)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path))
        self._db.row_factory = sqlite3.Row
        if self._db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
            self._db.close()
            raise ValueError("数据库版本高于当前软件支持范围，请使用匹配版本")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
        CREATE TABLE IF NOT EXISTS warehouses (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE);
        CREATE TABLE IF NOT EXISTS ports (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, port_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
          baud INTEGER NOT NULL, parity TEXT NOT NULL DEFAULT 'N', stop_bits INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS devices (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL,
          warehouse_id INTEGER NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
          port_id INTEGER NOT NULL REFERENCES ports(id) ON DELETE RESTRICT,
          address INTEGER NOT NULL, protocol TEXT NOT NULL, grain_count INTEGER NOT NULL,
          ambient_count INTEGER NOT NULL, calibration_version INTEGER NOT NULL DEFAULT 0,
          binding_version INTEGER NOT NULL DEFAULT 0, UNIQUE(port_id,address));
        CREATE TABLE IF NOT EXISTS calibrations (
          device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          kind TEXT NOT NULL, point INTEGER NOT NULL, temp_offset REAL NOT NULL,
          humidity_offset REAL NOT NULL, version INTEGER NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY(device_id,kind,point));
        CREATE TABLE IF NOT EXISTS thresholds (
          device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          kind TEXT NOT NULL, temp_low REAL, temp_high REAL, humidity_low REAL, humidity_high REAL,
          PRIMARY KEY(device_id,kind));
        CREATE TABLE IF NOT EXISTS bindings (
          device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          kind TEXT NOT NULL, point INTEGER NOT NULL, bus INTEGER NOT NULL, rom TEXT NOT NULL,
          position TEXT NOT NULL DEFAULT '', PRIMARY KEY(device_id,kind,point), UNIQUE(device_id,rom));
        CREATE TABLE IF NOT EXISTS binding_versions (
          device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
          version INTEGER NOT NULL, timestamp TEXT NOT NULL, config_json TEXT NOT NULL,
          PRIMARY KEY(device_id,version));
        CREATE TABLE IF NOT EXISTS samples (
          id INTEGER PRIMARY KEY, device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
          kind TEXT NOT NULL, point INTEGER NOT NULL, raw_temp REAL, raw_humidity REAL,
          temp REAL, humidity REAL, status TEXT NOT NULL, calibration_version INTEGER NOT NULL,
          binding_version INTEGER NOT NULL, temp_offset REAL NOT NULL, humidity_offset REAL NOT NULL,
          timestamp TEXT NOT NULL, source TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_samples_time ON samples(timestamp,id);
        CREATE INDEX IF NOT EXISTS ix_samples_device_time ON samples(device_id,timestamp,id);
        CREATE INDEX IF NOT EXISTS ix_samples_point_time ON samples(device_id,kind,point,timestamp,id);
        CREATE TABLE IF NOT EXISTS alarms (
          id INTEGER PRIMARY KEY, device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
          sample_id INTEGER REFERENCES samples(id) ON DELETE SET NULL,
          kind TEXT NOT NULL, point INTEGER NOT NULL, metric TEXT NOT NULL, direction TEXT NOT NULL,
          value REAL NOT NULL, threshold REAL NOT NULL, timestamp TEXT NOT NULL, source TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, acknowledged_at TEXT, cleared_at TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS ix_active_alarm ON alarms(device_id,kind,point,metric,direction,source)
          WHERE active=1;
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        PRAGMA user_version=1;
        """)
        self._db.commit()

    def close(self):
        self._db.close()

    def _rows(self, sql, values=()):
        return [dict(row) for row in self._db.execute(sql, values)]

    @staticmethod
    def _name(value, label="名称"):
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
            raise ValueError(f"{label}不能为空且不能超过200字")
        return value.strip()

    def _device(self, identifier):
        positive_int(identifier, "设备编号")
        row = self._db.execute("SELECT d.*,p.port_name FROM devices d JOIN ports p ON p.id=d.port_id WHERE d.id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("设备不存在")
        return dict(row)

    @staticmethod
    def _point(device, kind, point):
        kind = normalize_kind(kind)
        positive_int(point, "测点编号", device["grain_count" if kind == "grain" else "ambient_count"])
        return kind

    def warehouses(self):
        return self._rows("SELECT * FROM warehouses ORDER BY id")

    def save_warehouse(self, name: str, id: int | None = None) -> int:
        name = self._name(name, "仓房名称")
        with self._db:
            if id is None:
                return self._db.execute("INSERT INTO warehouses(name) VALUES(?)", (name,)).lastrowid
            positive_int(id, "仓房编号")
            if not self._db.execute("UPDATE warehouses SET name=? WHERE id=?", (name, id)).rowcount:
                raise ValueError("仓房不存在")
        return id

    def delete_warehouse(self, id):
        positive_int(id, "仓房编号")
        with self._db:
            self._db.execute("DELETE FROM warehouses WHERE id=?", (id,))

    def ports(self):
        rows = self._rows("SELECT * FROM ports ORDER BY id")
        for row in rows:
            row["source"] = source_for_port(row["port_name"])
        return rows

    def save_port(self, name: str, port_name: str, baud: int = 9600, id: int | None = None, parity: str = "N", stop_bits: int = 1) -> int:
        name, port_name = self._name(name), self._name(port_name, "串口")
        positive_int(baud, "波特率", 4_000_000)
        parity = str(parity).upper()
        if parity not in {"N", "E", "O"} or isinstance(stop_bits, bool) or stop_bits not in {1, 2}:
            raise ValueError("串口校验应为N/E/O，停止位应为1或2")
        with self._db:
            if id is None:
                return self._db.execute("INSERT INTO ports(name,port_name,baud,parity,stop_bits) VALUES(?,?,?,?,?)", (name, port_name, baud, parity, stop_bits)).lastrowid
            positive_int(id, "连接编号")
            previous = self._db.execute("SELECT port_name FROM ports WHERE id=?", (id,)).fetchone()
            if previous is None:
                raise ValueError("连接不存在")
            if source_for_port(previous[0]) != source_for_port(port_name):
                raise ValueError("模拟与串口连接不能互相转换，请新建连接和设备以保留来源")
            self._db.execute("UPDATE ports SET name=?,port_name=?,baud=?,parity=?,stop_bits=? WHERE id=?", (name, port_name, baud, parity, stop_bits, id))
        return id

    def delete_port(self, id):
        positive_int(id, "连接编号")
        with self._db:
            self._db.execute("DELETE FROM ports WHERE id=?", (id,))

    def devices(self):
        rows = self._rows("""SELECT d.*,w.name AS warehouse_name,p.name AS connection_name,
            p.port_name,p.baud,p.parity,p.stop_bits FROM devices d
            JOIN warehouses w ON w.id=d.warehouse_id JOIN ports p ON p.id=d.port_id ORDER BY d.id""")
        for row in rows:
            row["source"] = source_for_port(row["port_name"])
        return rows

    def save_device(self, name, warehouse_id, port_id, address=1, protocol="LC-M3", grain_count=10, ambient_count=1, id=None):
        name = self._name(name, "设备名称")
        positive_int(warehouse_id, "仓房编号")
        positive_int(port_id, "连接编号")
        positive_int(address, "设备地址", 247)
        if isinstance(grain_count, bool) or not isinstance(grain_count, int) or not 0 <= grain_count <= 165:
            raise ValueError("粮温点数应为0到165")
        if isinstance(ambient_count, bool) or not isinstance(ambient_count, int) or not 0 <= ambient_count <= 5:
            raise ValueError("仓温仓湿点数应为0到5")
        if grain_count + ambient_count == 0:
            raise ValueError("设备至少应有一个测点")
        protocol = {"LC": "LC-M3", "LC-M3": "LC-M3", "MODBUS": "Modbus RTU", "MODBUS RTU": "Modbus RTU"}.get(str(protocol).strip().upper())
        if protocol is None:
            raise ValueError("协议应为LC-M3或Modbus RTU")
        values = (name, warehouse_id, port_id, address, protocol, grain_count, ambient_count)
        with self._db:
            if id is None:
                return self._db.execute("""INSERT INTO devices(name,warehouse_id,port_id,address,protocol,grain_count,ambient_count)
                    VALUES(?,?,?,?,?,?,?)""", values).lastrowid
            previous = self._device(id)
            target = self._db.execute("SELECT port_name FROM ports WHERE id=?", (port_id,)).fetchone()
            if target is None:
                raise ValueError("连接不存在")
            if source_for_port(previous["port_name"]) != source_for_port(target[0]):
                raise ValueError("模拟设备与真实设备不能互换，请新建设备")
            for row in self.bindings(id) + self.calibrations(id):
                maximum = grain_count if row["kind"] == "grain" else ambient_count
                if row["point"] > maximum:
                    raise ValueError("新点数小于已有绑定或修正点号，请先处理配置")
            self._db.execute("""UPDATE devices SET name=?,warehouse_id=?,port_id=?,address=?,protocol=?,grain_count=?,ambient_count=? WHERE id=?""", values + (id,))
        return id

    def delete_device(self, id):
        self._device(id)
        if self._db.execute("SELECT 1 FROM samples WHERE device_id=? LIMIT 1", (id,)).fetchone() or self._db.execute("SELECT 1 FROM alarms WHERE device_id=? LIMIT 1", (id,)).fetchone():
            raise ValueError("设备已有历史或报警记录，不能删除；请保留以便追溯")
        with self._db:
            self._db.execute("DELETE FROM devices WHERE id=?", (id,))
            layouts = self.get_setting("point_layouts", {})
            if isinstance(layouts, dict) and str(id) in layouts:
                del layouts[str(id)]
                self._db.execute("UPDATE settings SET value=? WHERE key='point_layouts'",
                    (json.dumps(layouts, ensure_ascii=False, allow_nan=False),))

    def save_calibration(self, device_id, kind, point, temp_offset=0.0, humidity_offset=0.0):
        self.save_calibrations(device_id, [dict(kind=kind, point=point, temp_offset=temp_offset, humidity_offset=humidity_offset)])

    def save_calibrations(self, device_id, rows):
        device = self._device(device_id)
        if not isinstance(rows, list):
            raise ValueError("批量修正必须是列表")
        normalized, seen = [], set()
        existing = {(row["kind"], row["point"]): row for row in self.calibrations(device_id)}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("修正行必须是对象")
            kind = self._point(device, row.get("kind"), row.get("point"))
            key = kind, row["point"]
            if key in seen:
                raise ValueError("同一批量修正中测点重复")
            seen.add(key)
            t = finite_number(row.get("temp_offset", 0), "温度修正")
            h = finite_number(row.get("humidity_offset", 0), "湿度修正") if kind == "ambient" else 0.0
            old = existing.get(key)
            if old is not None and old["temp_offset"] == t and old["humidity_offset"] == h:
                continue
            normalized.append((kind, row["point"], t, h))
        if not normalized:
            return device["calibration_version"]
        with self._db:
            version = device["calibration_version"] + 1
            self._db.execute("UPDATE devices SET calibration_version=? WHERE id=?", (version, device_id))
            self._db.executemany("""INSERT INTO calibrations VALUES(?,?,?,?,?,?,?) ON CONFLICT(device_id,kind,point)
                DO UPDATE SET temp_offset=excluded.temp_offset,humidity_offset=excluded.humidity_offset,version=excluded.version,updated_at=excluded.updated_at""",
                [(device_id, kind, point, t, h, version, iso_timestamp()) for kind, point, t, h in normalized])
        return version

    def calibrations(self, device_id):
        self._device(device_id)
        return self._rows("SELECT * FROM calibrations WHERE device_id=? ORDER BY kind,point", (device_id,))

    def save_threshold(self, device_id, kind, temp_low=None, temp_high=None, humidity_low=None, humidity_high=None):
        self._device(device_id)
        kind = normalize_kind(kind)
        values = [finite_number(value, label, optional=True) for value, label in zip(
            (temp_low, temp_high, humidity_low, humidity_high), ("温度下限", "温度上限", "湿度下限", "湿度上限"))]
        if values[0] is not None and values[1] is not None and values[0] > values[1]:
            raise ValueError("温度下限不能高于上限")
        if values[2] is not None and values[3] is not None and values[2] > values[3]:
            raise ValueError("湿度下限不能高于上限")
        if any(value is not None and not 0 <= value <= 100 for value in values[2:]):
            raise ValueError("湿度阈值应在0到100之间")
        with self._db:
            self._db.execute("""INSERT INTO thresholds VALUES(?,?,?,?,?,?) ON CONFLICT(device_id,kind)
                DO UPDATE SET temp_low=excluded.temp_low,temp_high=excluded.temp_high,humidity_low=excluded.humidity_low,humidity_high=excluded.humidity_high""", (device_id, kind, *values))

    def thresholds(self, device_id):
        self._device(device_id)
        return self._rows("SELECT * FROM thresholds WHERE device_id=? ORDER BY kind", (device_id,))

    def save_samples(self, device_id: int, samples: list[dict], timestamp: str | None = None, source: str = "serial") -> list[dict]:
        return self._prepare_samples(device_id, samples, timestamp, source, persist=True)

    def preview_samples(self, device_id: int, samples: list[dict], timestamp: str | None = None, source: str = "serial") -> list[dict]:
        return self._prepare_samples(device_id, samples, timestamp, source, persist=False)

    def _prepare_samples(self, device_id, samples, timestamp, source, *, persist):
        device = self._device(device_id)
        source = normalize_source(source)
        if source != source_for_port(device["port_name"]):
            raise ValueError("采集来源与设备连接类型不符")
        stamp = iso_timestamp(timestamp)
        if not isinstance(samples, list):
            raise ValueError("采样数据必须是列表")
        offsets = {(r["kind"], r["point"]): r for r in self.calibrations(device_id)}
        results, seen = [], set()
        with self._db:
            for row in samples:
                if not isinstance(row, dict):
                    raise ValueError("采样行必须是对象")
                kind = self._point(device, row.get("kind"), row.get("point"))
                point = row["point"]
                if (kind, point) in seen:
                    raise ValueError("同一采样批次中测点重复")
                seen.add((kind, point))
                status = normalize_status(row.get("status", "unknown"))
                if "calibration_version" in row and "raw_temp" not in row and "raw_humidity" not in row:
                    raise ValueError("已修正数据必须提供原始值，避免重复修正")
                raw_t = finite_number(row.get("raw_temp", row.get("temp", row.get("temperature"))), "原始温度", optional=True)
                raw_h = finite_number(row.get("raw_humidity", row.get("humidity")), "原始湿度", optional=True)
                if kind == "grain":
                    raw_h = None
                if status == "ok" and (raw_t is None or (kind == "ambient" and raw_h is None)):
                    status = "data_error"
                if status == "ok" and (not -45 <= raw_t <= 130 or (raw_h is not None and not 0 <= raw_h <= 100)):
                    status = "data_error"
                offset = offsets.get((kind, point), {})
                t_offset, h_offset = offset.get("temp_offset", 0.0), offset.get("humidity_offset", 0.0)
                temp = raw_t + t_offset if status == "ok" and raw_t is not None else None
                humidity = raw_h + h_offset if status == "ok" and raw_h is not None else None
                if status == "ok" and (not -45 <= temp <= 130 or (humidity is not None and not 0 <= humidity <= 100)):
                    status, temp, humidity = "data_error", None, None
                data = dict(device_id=device_id, kind=kind, point=point, raw_temp=raw_t, raw_humidity=raw_h,
                    temp=temp, humidity=humidity, status=status, calibration_version=device["calibration_version"],
                    binding_version=device["binding_version"], temp_offset=t_offset, humidity_offset=h_offset,
                    timestamp=stamp, source=source)
                fields = tuple(data)
                data["id"] = self._db.execute(f"INSERT INTO samples({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", tuple(data.values())).lastrowid if persist else None
                data["temperature"] = temp
                data["device_name"] = device["name"]
                results.append(data)
        return results

    def _history_filter(self, device_id, start, end):
        start, end = time_range(start, end)
        clauses, values = [], []
        if device_id is not None:
            self._device(device_id)
            clauses.append("s.device_id=?")
            values.append(device_id)
        if start is not None:
            clauses.append("s.timestamp>=?")
            values.append(start)
        if end is not None:
            clauses.append("s.timestamp<=?")
            values.append(end)
        return " AND ".join(clauses) or "1", values

    def history(self, device_id=None, start=None, end=None, limit=5000):
        positive_int(limit, "查询条数", 1_000_000)
        where, values = self._history_filter(device_id, start, end)
        return self._rows(f"""SELECT s.*,d.name AS device_name,w.name AS warehouse_name FROM samples s
            JOIN devices d ON d.id=s.device_id JOIN warehouses w ON w.id=d.warehouse_id
            WHERE {where} ORDER BY s.timestamp DESC,s.id DESC LIMIT ?""", (*values, limit))

    def history_count(self, device_id=None, start=None, end=None):
        where, values = self._history_filter(device_id, start, end)
        return self._db.execute(f"SELECT COUNT(*) FROM samples s WHERE {where}", values).fetchone()[0]

    def _filtered_history(self, device_id=None, start=None, end=None, kind=None, point=None):
        where, values = self._history_filter(device_id, start, end)
        if kind is not None:
            kind = normalize_kind(kind)
            where += " AND s.kind=?"
            values.append(kind)
        if point is not None:
            positive_int(point, "测点编号", 5 if kind == "ambient" else 165)
            if device_id is not None and kind is not None:
                self._point(self._device(device_id), kind, point)
            where += " AND s.point=?"
            values.append(point)
        return where, values

    def history_page(self, device_id=None, start=None, end=None, kind=None, point=None, limit=500, offset=0):
        positive_int(limit, "每页条数", 10_000)
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 2**63 - 1:
            raise ValueError("分页偏移必须是非负整数")
        where, values = self._filtered_history(device_id, start, end, kind, point)
        return self._rows(f"""SELECT s.*,d.name AS device_name,w.name AS warehouse_name FROM samples s
            JOIN devices d ON d.id=s.device_id JOIN warehouses w ON w.id=d.warehouse_id
            WHERE {where} ORDER BY s.timestamp DESC,s.id DESC LIMIT ? OFFSET ?""", (*values, limit, offset))

    def history_count_filters(self, device_id=None, start=None, end=None, kind=None, point=None):
        where, values = self._filtered_history(device_id, start, end, kind, point)
        return self._db.execute(f"SELECT COUNT(*) FROM samples s WHERE {where}", values).fetchone()[0]

    def export_history(self, destination, device_id=None, start=None, end=None, kind=None, point=None):
        from .domain import HISTORY_COLUMNS, export_csv, _spreadsheet_value
        path = Path(destination)
        if path.suffix.lower() not in {".csv", ".xlsx"}:
            raise ValueError("导出文件应为.csv或.xlsx")
        if path.resolve() == self.path.resolve():
            raise ValueError("导出不能覆盖当前数据库")
        where, values = self._filtered_history(device_id, start, end, kind, point)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".warehouse-report-", suffix=path.suffix, dir=path.parent)
        os.close(handle)
        count, cursor, book = 0, None, None
        try:
            cursor = self._db.execute(f"""SELECT s.*,d.name AS device_name,w.name AS warehouse_name FROM samples s
                JOIN devices d ON d.id=s.device_id JOIN warehouses w ON w.id=d.warehouse_id
                WHERE {where} ORDER BY s.timestamp,s.id""", values)
            if path.suffix.lower() == ".csv":
                def rows():
                    nonlocal count
                    for row in cursor:
                        count += 1
                        yield dict(row)
                export_csv(temporary, rows())
            else:
                try:
                    from openpyxl import Workbook
                except ImportError as exc:
                    raise RuntimeError("Excel导出需要安装openpyxl") from exc
                book = Workbook(write_only=True)
                sheet = book.create_sheet("采集记录1")
                sheet.append(list(HISTORY_COLUMNS))
                for row in cursor:
                    if count != 0 and count % 1_048_575 == 0:
                        sheet = book.create_sheet(f"采集记录{count // 1_048_575 + 1}")
                        sheet.append(list(HISTORY_COLUMNS))
                    values_row = dict(row)
                    sheet.append([_spreadsheet_value(values_row.get(field)) for field in HISTORY_COLUMNS])
                    count += 1
                book.save(temporary)
            os.replace(temporary, path)
        finally:
            if cursor is not None:
                cursor.close()
            if book is not None:
                with suppress(Exception):
                    for sheet in book:
                        if not sheet.closed:
                            sheet.close()
                    book.close()
            if os.path.exists(temporary):
                os.unlink(temporary)
        return count

    def latest_samples(self, device_id=None):
        if device_id is not None:
            self._device(device_id)
        return self._rows("""SELECT s.*,d.name AS device_name,w.name AS warehouse_name FROM samples s
            JOIN devices d ON d.id=s.device_id JOIN warehouses w ON w.id=d.warehouse_id
            WHERE (? IS NULL OR s.device_id=?) AND NOT EXISTS (
              SELECT 1 FROM samples newer WHERE newer.device_id=s.device_id AND newer.kind=s.kind
              AND newer.point=s.point AND (newer.timestamp>s.timestamp OR
                (newer.timestamp=s.timestamp AND newer.id>s.id))) ORDER BY s.device_id,s.kind,s.point""", (device_id, device_id))

    def clear_history(self, start=None, end=None, device_id=None, dry_run=False):
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run必须为布尔值")
        where, values = self._history_filter(device_id, start, end)
        with self._db:
            count = self._db.execute(f"SELECT COUNT(*) FROM samples s WHERE {where}", values).fetchone()[0]
            if not dry_run:
                self._db.execute(f"DELETE FROM samples AS s WHERE {where}", values)
        return count

    def evaluate_alarms(self, device_id, rows):
        device = self._device(device_id)
        thresholds = {row["kind"]: row for row in self.thresholds(device_id)}
        created = []
        with self._db:
            for row in rows:
                if row.get("device_id", device_id) != device_id:
                    raise ValueError("报警数据来自其他设备")
                kind = self._point(device, row.get("kind"), row.get("point"))
                if normalize_status(row.get("status", "unknown")) != "ok":
                    continue
                source = normalize_source(row.get("source", source_for_port(device["port_name"])))
                if source != source_for_port(device["port_name"]):
                    raise ValueError("报警来源与设备不符")
                threshold = thresholds.get(kind)
                if threshold is None:
                    continue
                stamp = iso_timestamp(row.get("timestamp"))
                for metric in ("temp", "humidity"):
                    value = finite_number(row.get(metric, row.get("temperature") if metric == "temp" else None), metric, optional=True)
                    if value is None:
                        continue
                    for direction in ("low", "high"):
                        bound = threshold[f"{metric}_{direction}"]
                        triggered = bound is not None and (value < bound if direction == "low" else value > bound)
                        key = (device_id, kind, row["point"], metric, direction, source)
                        current = self._db.execute("""SELECT id FROM alarms WHERE device_id=? AND kind=? AND point=?
                            AND metric=? AND direction=? AND source=? AND active=1""", key).fetchone()
                        if not triggered:
                            if current is not None:
                                self._db.execute("UPDATE alarms SET active=0,cleared_at=? WHERE id=?", (stamp, current[0]))
                            continue
                        if current is not None:
                            continue
                        sample_id = row.get("id")
                        if sample_id is not None and self._db.execute("SELECT 1 FROM samples WHERE id=? AND device_id=? AND source=?", (sample_id, device_id, source)).fetchone() is None:
                            raise ValueError("报警采样记录不存在或来源不符")
                        cursor = self._db.execute("""INSERT INTO alarms(device_id,sample_id,kind,point,metric,direction,value,threshold,timestamp,source)
                            VALUES(?,?,?,?,?,?,?,?,?,?)""", (device_id, sample_id, kind, row["point"], metric, direction, value, bound, stamp, source))
                        created.append(dict(self._db.execute("SELECT * FROM alarms WHERE id=?", (cursor.lastrowid,)).fetchone()))
        return created

    def alarms(self, limit=500):
        positive_int(limit, "报警条数", 100_000)
        return self._rows("""SELECT a.*,d.name AS device_name,w.name AS warehouse_name FROM alarms a
            JOIN devices d ON d.id=a.device_id JOIN warehouses w ON w.id=d.warehouse_id
            ORDER BY (a.active=1 AND a.acknowledged_at IS NULL) DESC,
                a.active DESC,a.timestamp DESC,a.id DESC LIMIT ?""", (limit,))

    def acknowledge_alarm(self, id):
        positive_int(id, "报警编号")
        with self._db:
            if not self._db.execute("UPDATE alarms SET acknowledged_at=COALESCE(acknowledged_at,?) WHERE id=?", (iso_timestamp(), id)).rowcount:
                raise ValueError("报警记录不存在")

    def acknowledge_alarms(self, ids=None):
        values = [iso_timestamp()]
        where = "active=1 AND acknowledged_at IS NULL"
        if ids is not None:
            if not isinstance(ids, list):
                raise ValueError("报警编号必须是列表")
            ids = list(dict.fromkeys(positive_int(item, "报警编号") for item in ids))
            if not ids:
                return 0
            if len(ids) > 10_000:
                raise ValueError("一次最多确认10000条报警")
            where += f" AND id IN ({','.join('?' for _ in ids)})"
            values.extend(ids)
        with self._db:
            return self._db.execute(f"UPDATE alarms SET acknowledged_at=? WHERE {where}", values).rowcount

    def bindings(self, device_id):
        self._device(device_id)
        return self._rows("SELECT * FROM bindings WHERE device_id=? ORDER BY kind,point", (device_id,))

    def get_binding_version(self, device_id):
        return self._device(device_id)["binding_version"]

    def _replace_bindings(self, device_id, rows, version):
        self._db.execute("DELETE FROM bindings WHERE device_id=?", (device_id,))
        self._db.executemany("INSERT INTO bindings(device_id,kind,point,bus,rom,position) VALUES(?,?,?,?,?,?)",
            [(device_id, row["kind"], row["point"], row["bus"], row["rom"], row["position"]) for row in rows])
        self._db.execute("UPDATE devices SET binding_version=? WHERE id=?", (version, device_id))
        self._db.execute("INSERT INTO binding_versions VALUES(?,?,?,?)", (device_id, version, iso_timestamp(), json.dumps(rows, ensure_ascii=False)))

    def set_bindings(self, device_id, rows):
        device = self._device(device_id)
        normalized, points, roms = [], set(), set()
        if not isinstance(rows, list):
            raise ValueError("绑定配置必须是列表")
        for row in rows:
            kind = self._point(device, row.get("kind", row.get("type")), row.get("point"))
            bus = row.get("bus", 0)
            if isinstance(bus, bool) or not isinstance(bus, int) or not 0 <= bus <= 255:
                raise ValueError("支路编号应为0到255")
            rom = rom_hex(row.get("rom"))
            if (kind, row["point"]) in points or rom in roms:
                raise ValueError("绑定测点或传感器编码重复")
            points.add((kind, row["point"]))
            roms.add(rom)
            normalized.append(dict(kind=kind, point=row["point"], bus=bus, rom=rom, position=str(row.get("position", ""))))
        normalized.sort(key=lambda row: (row["kind"], row["point"]))
        old = [{key: row[key] for key in ("kind", "point", "bus", "rom", "position")} for row in self.bindings(device_id)]
        if normalized == old:
            return device["binding_version"]
        with self._db:
            self._replace_bindings(device_id, normalized, device["binding_version"] + 1)
        return device["binding_version"] + 1

    def bump_binding_version(self, device_id):
        device = self._device(device_id)
        with self._db:
            rows = [{key: row[key] for key in ("kind", "point", "bus", "rom", "position")} for row in self.bindings(device_id)]
            self._replace_bindings(device_id, rows, device["binding_version"] + 1)
        return device["binding_version"] + 1

    def set_setting(self, key, value):
        key = self._name(key, "设置键")
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        with self._db:
            self._db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encoded))

    def get_setting(self, key, default=None):
        row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def backup(self, destination):
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.resolve() == self.path.resolve():
            raise ValueError("备份路径不能与当前数据库相同")
        handle, temporary = tempfile.mkstemp(prefix=".warehouse-backup-", suffix=".sqlite3", dir=path.parent)
        os.close(handle)
        try:
            target = sqlite3.connect(temporary)
            try:
                self._db.backup(target)
            finally:
                target.close()
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def restore_backup(self, source, expected_source=None):
        expected_source = normalize_source(expected_source) if expected_source is not None else None
        path = Path(source)
        if not path.is_file():
            raise ValueError("备份数据库不存在")
        if path.resolve() == self.path.resolve() or (self.path.exists() and os.path.samefile(path, self.path)):
            raise ValueError("不能用当前数据库恢复自身")
        backup = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)
        try:
            backup.execute("PRAGMA query_only=ON")
            backup.execute("PRAGMA trusted_schema=OFF")
            backup.execute("BEGIN")
            if backup.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("备份数据库版本不兼容")
            if backup.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("备份数据库完整性检查失败")
            if backup.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("备份数据库存在无效关联")
            def schema(connection):
                return {row[0]: (row[1], " ".join(row[2].split())) for row in connection.execute(
                    "SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL")}
            if schema(backup) != schema(self._db):
                raise ValueError("备份表结构与当前软件不兼容")
            sources = {source_for_port(row[0]) for row in backup.execute("SELECT port_name FROM ports")}
            for table in ("samples", "alarms"):
                for recorded, port_name in backup.execute(f"SELECT DISTINCT r.source,p.port_name FROM {table} r JOIN devices d ON d.id=r.device_id JOIN ports p ON p.id=d.port_id"):
                    normalized = normalize_source(recorded)
                    if normalized != recorded or normalized != source_for_port(port_name):
                        raise ValueError("备份数据的模拟/串口来源存在冲突")
                    sources.add(normalized)
            if expected_source is not None and sources - {expected_source}:
                raise ValueError("备份来源与当前模拟/真实工作区不匹配")
            # The source stays in one read transaction from validation through
            # backup. SQLite commits the destination backup atomically.
            backup.backup(self._db)
            self._db.execute("PRAGMA foreign_keys=ON")
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"备份数据库无法恢复：{exc}") from exc
        finally:
            backup.close()

    def export_config(self):
        return dict(format="warehouse-pc", version=1, warehouses=self.warehouses(), ports=self.ports(), devices=self.devices(),
            calibrations=self._rows("SELECT * FROM calibrations ORDER BY device_id,kind,point"),
            thresholds=self._rows("SELECT * FROM thresholds ORDER BY device_id,kind"),
            bindings=self._rows("SELECT * FROM bindings ORDER BY device_id,kind,point"),
            settings={row["key"]: json.loads(row["value"]) for row in self._db.execute("SELECT * FROM settings")})

    def _merge_imported_layouts(self, layouts, device_ids):
        if not isinstance(layouts, dict):
            raise ValueError("测点布置必须是按设备编号组织的对象")
        merged = self.get_setting("point_layouts", {})
        if not isinstance(merged, dict):
            raise ValueError("当前测点布置格式无效，无法安全合并")
        for source_key, source_points in layouts.items():
            if not isinstance(source_key, str) or not source_key.isdecimal():
                raise ValueError("测点布置设备键必须是设备编号字符串")
            source_id = int(source_key)
            if str(source_id) != source_key or source_id not in device_ids:
                raise ValueError("测点布置引用了未导入的设备")
            if not isinstance(source_points, dict):
                raise ValueError("设备测点布置必须是对象")
            target_id = device_ids[source_id]
            device = self._device(target_id)
            target_key = str(target_id)
            existing_points = merged.get(target_key, {})
            if not isinstance(existing_points, dict):
                raise ValueError("当前设备测点布置格式无效，无法安全合并")
            normalized = dict(existing_points)
            for point_key, position in source_points.items():
                if not isinstance(point_key, str) or point_key.count(":") != 1:
                    raise ValueError("测点布置键应为grain:编号或ambient:编号")
                kind, point_string = point_key.split(":")
                if kind not in {"grain", "ambient"} or not point_string.isdecimal():
                    raise ValueError("测点布置的类型或编号无效")
                point = int(point_string)
                if str(point) != point_string:
                    raise ValueError("测点布置编号格式无效")
                self._point(device, kind, point)
                if not isinstance(position, dict) or set(position) - {"line", "layer", "position"}:
                    raise ValueError("测点位置必须包含line/layer/position字符串字段")
                if any(not isinstance(position.get(field, ""), str) for field in ("line", "layer", "position")):
                    raise ValueError("测点线号、层号和位置必须是字符串")
                normalized[point_key] = {field: position.get(field, "") for field in ("line", "layer", "position")}
            merged[target_key] = normalized
        return merged

    def import_config(self, data):
        """Validate transactionally and merge by warehouse/port/device endpoint.

        Unrelated configuration and every historical sample/alarm are retained.
        IDs in the exported file are references, not local replacement IDs.
        """
        if not isinstance(data, dict) or data.get("format") != "warehouse-pc" or data.get("version") != 1:
            raise ValueError("不支持的配置格式或版本")
        for field in ("warehouses", "ports", "devices", "calibrations", "thresholds", "bindings"):
            if not isinstance(data.get(field, []), list):
                raise ValueError(f"配置{field}必须是列表")
        if not isinstance(data.get("settings", {}), dict):
            raise ValueError("配置settings必须是对象")
        # Existing public methods commit individually, so use a savepoint and
        # suppress connection context commits during this multi-step operation.
        original = self._db
        self._db = _TransactionConnection(original)
        original.execute("SAVEPOINT import_configuration")
        try:
            warehouse_ids, port_ids, device_ids = {}, {}, {}
            for row in data["warehouses"]:
                old_id = positive_int(row["id"], "导入仓房编号")
                if old_id in warehouse_ids:
                    raise ValueError("导入仓房编号重复")
                existing = original.execute("SELECT id FROM warehouses WHERE name=? COLLATE NOCASE", (self._name(row["name"]),)).fetchone()
                warehouse_ids[old_id] = self.save_warehouse(row["name"], existing[0] if existing else None)
            for row in data["ports"]:
                old_id = positive_int(row["id"], "导入连接编号")
                if old_id in port_ids:
                    raise ValueError("导入连接编号重复")
                existing = original.execute("SELECT id FROM ports WHERE port_name=? COLLATE NOCASE", (self._name(row["port_name"]),)).fetchone()
                port_ids[old_id] = self.save_port(row["name"], row["port_name"], row.get("baud", 9600), existing[0] if existing else None, row.get("parity", "N"), row.get("stop_bits", 1))
            for row in data["devices"]:
                old_id = positive_int(row["id"], "导入设备编号")
                if old_id in device_ids:
                    raise ValueError("导入设备编号重复")
                port_id = port_ids[row["port_id"]]
                existing = original.execute("SELECT id FROM devices WHERE port_id=? AND address=?", (port_id, row["address"])).fetchone()
                device_ids[old_id] = self.save_device(row["name"], warehouse_ids[row["warehouse_id"]], port_id,
                    row.get("address", 1), row.get("protocol", "LC-M3"), row.get("grain_count", 10), row.get("ambient_count", 1), existing[0] if existing else None)
            seen_calibrations, seen_thresholds = set(), set()
            for row in data.get("calibrations", []):
                key = (row["device_id"], row["kind"], row["point"])
                if key in seen_calibrations:
                    raise ValueError("导入修正点重复")
                seen_calibrations.add(key)
                self.save_calibration(device_ids[row["device_id"]], row["kind"], row["point"], row.get("temp_offset", 0), row.get("humidity_offset", 0))
            for row in data.get("thresholds", []):
                key = (row["device_id"], row["kind"])
                if key in seen_thresholds:
                    raise ValueError("导入报警阈值重复")
                seen_thresholds.add(key)
                self.save_threshold(device_ids[row["device_id"]], row["kind"], row.get("temp_low"), row.get("temp_high"), row.get("humidity_low"), row.get("humidity_high"))
            for old_id, new_id in device_ids.items():
                self.set_bindings(new_id, [row for row in data.get("bindings", []) if row["device_id"] == old_id])
            if any(row["device_id"] not in device_ids for row in data.get("bindings", [])):
                raise ValueError("绑定配置引用不存在的设备")
            for key, value in data.get("settings", {}).items():
                if key == "point_layouts":
                    value = self._merge_imported_layouts(value, device_ids)
                self.set_setting(key, value)
            original.execute("RELEASE SAVEPOINT import_configuration")
        except Exception as exc:
            original.execute("ROLLBACK TO SAVEPOINT import_configuration")
            original.execute("RELEASE SAVEPOINT import_configuration")
            if isinstance(exc, (KeyError, TypeError)):
                raise ValueError("配置字段缺失、引用错误或类型不正确") from exc
            raise
        finally:
            self._db = original


class _TransactionConnection:
    """Forward SQL while nested Store mutators share the import savepoint."""
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        return False

"""Separate numbering ledger and active batch assignments."""

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class NumberingRecord:
    id: int
    batch: str
    rom_hex: str
    old_root: int
    old_point: int
    root: int
    point: int
    status: str
    created_at: str
    updated_at: str
    note: str
    advance: bool
    configuration: int

    @property
    def next_point(self):
        return self.point + 1 if self.status == "verified" and self.advance and self.point < 254 else None


class NumberingStorage:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY,
                batch TEXT NOT NULL COLLATE NOCASE,
                rom_hex TEXT NOT NULL,
                old_root INTEGER NOT NULL,
                old_point INTEGER NOT NULL,
                root INTEGER NOT NULL CHECK(root BETWEEN 1 AND 254),
                point INTEGER NOT NULL CHECK(point BETWEEN 1 AND 254),
                status TEXT NOT NULL CHECK(status IN ('pending','verified','failed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                note TEXT NOT NULL,
                advance INTEGER NOT NULL CHECK(advance IN (0,1)),
                configuration INTEGER NOT NULL
            );
            DROP INDEX IF EXISTS one_pending;
            CREATE UNIQUE INDEX IF NOT EXISTS pending_slot ON records(batch,root,point) WHERE status='pending';
            CREATE UNIQUE INDEX IF NOT EXISTS pending_rom ON records(rom_hex) WHERE status='pending';
            CREATE TABLE IF NOT EXISTS assignments (
                rom_hex TEXT PRIMARY KEY,
                batch TEXT NOT NULL COLLATE NOCASE,
                root INTEGER NOT NULL,
                point INTEGER NOT NULL,
                record_id INTEGER NOT NULL REFERENCES records(id),
                UNIQUE(batch,root,point)
            );
        """)

    def close(self):
        self.db.close()

    @staticmethod
    def _record(row):
        if row is None:
            return None
        data = dict(row)
        data["advance"] = bool(data["advance"])
        return NumberingRecord(**data)

    def record(self, identifier):
        return self._record(self.db.execute("SELECT * FROM records WHERE id=?", (identifier,)).fetchone())

    def pending(self):
        return self._record(self.db.execute("SELECT * FROM records WHERE status='pending' ORDER BY id LIMIT 1").fetchone())

    def verified_assignment(self, rom_hex):
        return self._record(self.db.execute(
            "SELECT records.* FROM assignments JOIN records ON records.id=record_id WHERE assignments.rom_hex=?",
            (rom_hex,),
        ).fetchone())

    def begin(self, batch, probe, root, point, advance, renumber):
        if not isinstance(batch, str):
            raise ValueError("请输入批次名称。")
        batch = batch.strip()
        if not batch or len(batch) > 120 or any(ord(character) < 32 for character in batch):
            raise ValueError("请输入 1～120 个字符的批次名称。")
        if any(type(value) is not int or not 1 <= value <= 254 for value in (root, point)):
            raise ValueError("根号和点号必须在 1～254 范围内。")
        stamp = _timestamp()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            pending = self.db.execute("""SELECT 1 FROM records WHERE status='pending'
                AND (rom_hex=? OR (batch=? AND root=? AND point=?))""", (probe.rom_hex, batch, root, point)).fetchone()
            if pending:
                raise ValueError("该探头或编号有待核验记录，请先完成对应复核。")
            occupant = self.db.execute("SELECT rom_hex FROM assignments WHERE batch=? AND root=? AND point=?",
                                       (batch, root, point)).fetchone()
            if occupant is not None and occupant[0] != probe.rom_hex:
                raise ValueError("该批次的根号、点号已分配给另一只探头。")
            previous = self.verified_assignment(probe.rom_hex)
            if previous is not None:
                same_assignment = (occupant is not None and (probe.root, probe.point) == (root, point)
                                   and probe.configuration == previous.configuration)
                if same_assignment:
                    raise ValueError("这只探头的相同编号已经验证。")
                if not renumber:
                    raise ValueError("这只探头已有编号记录。请确认改号后再写入。")
            identifier = self.db.execute("""INSERT INTO records (
                batch,rom_hex,old_root,old_point,root,point,status,created_at,updated_at,note,advance,configuration
            ) VALUES (?,?,?,?,?,?,'pending',?,?,?,?,?)""",
                (batch, probe.rom_hex, probe.root, probe.point, root, point, stamp, stamp,
                 "已准备写入，结果待核验。", int(bool(advance)), probe.configuration)).lastrowid
        return self.record(identifier)

    def update_pending(self, identifier, note):
        with self.db:
            self.db.execute("UPDATE records SET updated_at=?,note=? WHERE id=? AND status='pending'",
                            (_timestamp(), note, identifier))
        return self.record(identifier)

    def finish(self, identifier, verified, note):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            record = self.record(identifier)
            if record is None or record.status != "pending":
                raise ValueError("编号记录已变化，请重新读取。")
            if verified:
                self.db.execute("""INSERT INTO assignments (rom_hex,batch,root,point,record_id) VALUES (?,?,?,?,?)
                    ON CONFLICT(rom_hex) DO UPDATE SET batch=excluded.batch,root=excluded.root,
                        point=excluded.point,record_id=excluded.record_id""",
                    (record.rom_hex, record.batch, record.root, record.point, identifier))
            self.db.execute("UPDATE records SET status=?,updated_at=?,note=? WHERE id=?",
                            ("verified" if verified else "failed", _timestamp(), note, identifier))
        return self.record(identifier)

    def records(self, batch=None):
        if batch is None:
            rows = self.db.execute("SELECT * FROM records ORDER BY id DESC")
        else:
            rows = self.db.execute("SELECT * FROM records WHERE batch=? ORDER BY id DESC", (batch,))
        return [self._record(row) for row in rows]

    def batches(self):
        return [row[0] for row in self.db.execute("SELECT DISTINCT batch FROM records ORDER BY batch")]

    def export_csv(self, path, batch=None):
        path = Path(path)
        if path.resolve() == self.path.resolve():
            raise ValueError("导出文件不能覆盖编号数据库。")
        rows = self.records(batch)
        temporary = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8-sig", newline="", dir=path.parent, delete=False) as stream:
                temporary = stream.name
                writer = csv.writer(stream)
                writer.writerow(("批次", "ROM", "原根号", "原点号", "新根号", "新点号", "状态", "创建时间", "更新时间", "备注"))
                for row in rows:
                    writer.writerow((self._spreadsheet_text(row.batch), row.rom_hex, row.old_root, row.old_point,
                                     row.root, row.point, row.status, row.created_at, row.updated_at,
                                     self._spreadsheet_text(row.note)))
            os.replace(temporary, path)
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
        return len(rows)

    @staticmethod
    def _spreadsheet_text(text):
        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text

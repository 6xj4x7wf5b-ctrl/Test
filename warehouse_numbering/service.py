"""Numbering workflow, including write recovery and physical power-cycle verification."""

from dataclasses import dataclass, replace
from pathlib import Path
import secrets
import sqlite3
import time

from PySide6.QtCore import QObject, QTimer, Signal

from .protocol import CAPABILITY, READ_SINGLE, WRITE_NUMBER, FixtureError, ProbeReading, write_payload
from .simulator import SimulatedFixture
from .storage import NumberingRecord, NumberingStorage
from .transport import FixtureTransport

POWER_OFF_SECONDS = 2.0  # Provisional until the fixture/probe combination is measured.
POLL_INTERVAL_MS = 650


@dataclass(frozen=True, slots=True)
class NumberingState:
    phase: str = "disconnected"
    message: str = ""
    connected: bool = False
    fixture: bool = False
    probe: ProbeReading | None = None
    pending: NumberingRecord | None = None
    busy: bool = False


class NumberingService(QObject):
    state_changed = Signal(object)
    completed = Signal(object)
    records_changed = Signal()

    def __init__(self, data_dir: Path, *, demo=False, parent=None):
        super().__init__(parent)
        self.demo = demo
        data_dir = Path(data_dir)
        self.storage = NumberingStorage(data_dir / ("numbering-demo.db" if demo else "numbering.db"))
        self.transport = SimulatedFixture(data_dir, self) if demo else FixtureTransport(self)
        self.transport.disconnected.connect(self._connection_lost)
        self.state = NumberingState(pending=self.storage.pending())
        self.address = 1
        self._closed = False
        self._epoch = 0
        self._off_since = None
        self._checking_probe = False
        self._stop_after_read = False
        self._poll = QTimer(self)
        self._poll.setSingleShot(True)
        self._poll.timeout.connect(self._verification_step)

    def _publish(self, **changes):
        self.state = replace(self.state, **changes)
        self.state_changed.emit(self.state)

    def available_ports(self):
        return self.transport.available_ports()

    def connect_fixture(self, port_name="", *, address=1, baud=1200, parity="N", stop_bits=1):
        if self._closed or self.state.connected:
            return
        if (type(address) is not int or not 1 <= address <= 247 or type(baud) is not int or not 1 <= baud <= 115200
                or parity not in ("N", "E", "O") or type(stop_bits) is not int or stop_bits not in (1, 2)
                or (not self.demo and (not port_name or port_name.strip().upper() == "SIMULATOR"))):
            self._publish(phase="error", message="请检查串口参数和工装地址。")
            return
        self.address = address
        try:
            self.transport.open(port_name, baud, parity, stop_bits)
        except (ValueError, OSError) as error:
            self._publish(phase="error", message=str(error))
            return
        self._publish(connected=True, phase="checking", busy=True, message="正在确认编号工装能力…")

        def checked(_value, error):
            if error is not None:
                self._publish(phase="error", fixture=False, busy=False, message=str(error))
                return
            pending = self.state.pending
            self._publish(fixture=True, busy=False, phase="pending" if pending else "ready",
                          message="检测到待核验记录，请接回原探头并开始断电复核。" if pending else "工装已确认，请接入一只探头并读取。")

        self._request(CAPABILITY, b"", None, checked)

    def disconnect_fixture(self):
        self._disconnect("")

    def _connection_lost(self, message):
        self._disconnect(f"串口已断开。{message}")

    def _disconnect(self, message):
        self._epoch += 1
        self._poll.stop()
        self._off_since = None
        self._checking_probe = self._stop_after_read = False
        self.transport.close()
        pending = self.state.pending
        if pending is not None:
            message = (message + " " if message else "") + "保留待核验记录。重新连接后读取复核。"
        self._publish(phase="disconnected", connected=False, fixture=False, probe=None, busy=False, message=message)

    def _request(self, op, payload, token, callback):
        epoch = self._epoch

        def deliver(value, error):
            if not self._closed and epoch == self._epoch:
                callback(value, error)

        try:
            self.transport.request(self.address, op, payload, token, deliver)
        except (ValueError, OSError) as error:
            deliver(None, error)

    def _read(self, callback):
        token = secrets.token_bytes(4)
        self._request(READ_SINGLE, token, token, callback)

    def read_probe(self):
        if not self.state.fixture or self.state.busy:
            return
        self._publish(phase="reading", busy=True, probe=None, message="正在读取探头…")

        def read(probe, error):
            if error is not None:
                self._publish(phase="pending" if self.state.pending else "error", busy=False, message=str(error))
            else:
                self._publish(phase="pending" if self.state.pending else "ready", busy=False, probe=probe,
                              message="探头 CRC 校验通过。")

        self._read(read)

    def write_number(self, batch, root, point, *, renumber=False, advance=True):
        state = self.state
        if (not state.fixture or state.busy or state.pending is not None or state.probe is None
                or state.phase not in ("ready", "verified")):
            return
        probe = state.probe
        token = secrets.token_bytes(4)
        try:
            payload = write_payload(token, probe, root, point)
            record = self.storage.begin(batch, probe, root, point, advance, renumber)
        except (ValueError, sqlite3.Error) as error:
            # Keep the validated read available so an input error can be corrected.
            self._publish(message=str(error))
            return
        self._publish(phase="writing", pending=record, busy=True, message="正在写入并回读校验…")
        self.records_changed.emit()

        def written(value, error):
            if error is not None:
                note = f"{error} 结果待核验，请先读取原探头。"
                self._set_pending(record, note, probe=None)
                return
            if not self._matches_probe(record, value) or (value.root, value.point) != (root, point):
                self._set_pending(record, "回读与目标不一致。请重新连接原探头并进行断电复核。", probe=None)
                return
            self._set_pending(record, "回读通过，待断电复核。", probe=value)

        if (probe.root, probe.point) == (root, point):
            self._set_pending(record, "当前读值与目标相同。请断电复核确认。", probe=probe)
        else:
            self._request(WRITE_NUMBER, payload, token, written)

    @staticmethod
    def _matches_probe(record, probe):
        return (probe is not None and probe.rom_hex == record.rom_hex and probe.configuration == record.configuration)

    def _set_pending(self, record, note, probe=None):
        try:
            record = self.storage.update_pending(record.id, note)
        except sqlite3.Error as error:
            note = f"{note} 编号记录更新失败。{error}"
        self._publish(phase="pending", pending=record, busy=False, probe=probe, message=note)
        self.records_changed.emit()

    def resume_record(self, record_id):
        if self.state.busy:
            return
        try:
            record = self.storage.record(record_id)
        except sqlite3.Error as error:
            self._publish(message=f"读取编号记录失败。{error}")
            return
        if record is not None and record.status == "pending":
            self._publish(phase="pending" if self.state.fixture else "disconnected", pending=record, probe=None,
                          message="已载入待核验记录。接回原探头后开始断电复核。")

    def defer_record(self):
        if self.state.pending is None or self.state.busy:
            return
        self._publish(phase="ready" if self.state.fixture else "disconnected", pending=None, probe=None,
                      message="已保留待核验记录，可接入下一只探头。")

    def verify_power_cycle(self):
        if not self.state.fixture or self.state.busy or self.state.pending is None:
            return
        self._off_since = None
        self._publish(phase="wait_remove", busy=True, probe=None, message="正在读取原探头，准备断电复核…")
        self._checking_probe = True

        def baseline(probe, error):
            if self._verification_stopped():
                return
            record = self.state.pending
            if error is not None or not self._matches_probe(record, probe):
                message = str(error) if error is not None else "请接入记录中的原探头，并核对配置字节。"
                self._publish(phase="pending", busy=False, message=message)
                return
            self._prompt_remove(probe)
            self._poll.start(POLL_INTERVAL_MS)

        self._read(baseline)

    def _prompt_remove(self, probe=None):
        self._publish(phase="wait_remove", probe=probe,
                      message=f"请完全拔下原探头，等待至少 {POWER_OFF_SECONDS:g} 秒，收到重新接入提示后再插回。")

    def stop_verification(self):
        if self.state.phase not in ("wait_remove", "wait_insert"):
            return
        self._poll.stop()
        self._off_since = None
        if self._checking_probe:
            self._stop_after_read = True
        else:
            self._publish(phase="pending", busy=False, message="已暂停断电复核。保留待核验记录。")

    def _verification_stopped(self):
        self._checking_probe = False
        if self._stop_after_read:
            self._stop_after_read = False
            self._publish(phase="pending", busy=False, message="已暂停断电复核。保留待核验记录。")
            return True
        return False

    def _verification_step(self):
        if not self.state.fixture or self.state.pending is None or self.state.phase not in ("wait_remove", "wait_insert"):
            return
        self._checking_probe = True
        self._read(self._verification_reply)

    def _verification_reply(self, probe, error):
        if self._verification_stopped():
            return
        record = self.state.pending
        if isinstance(error, FixtureError) and error.result == 7:
            if self._off_since is None:
                self._off_since = time.monotonic()
            elif time.monotonic() - self._off_since >= POWER_OFF_SECONDS:
                self._publish(phase="wait_insert", probe=None, message="已确认拔下。请重新接入同一只探头。")
        elif error is not None:
            self._off_since = None
            self._publish(phase="pending", busy=False, probe=None,
                          message=f"{error} 请重新开始断电复核。")
            return
        elif self.state.phase == "wait_insert":
            if not self._matches_probe(record, probe):
                self._off_since = None
                self._publish(phase="pending", busy=False, probe=None,
                              message="接入的 ROM 或配置字节不匹配。请重新接入原探头并开始复核。")
                return
            self._finish_verification(record, probe)
            return
        else:
            self._off_since = None
            if not self._matches_probe(record, probe):
                self._publish(phase="pending", busy=False, probe=None, message="请接回原探头并重新开始断电复核。")
                return
            self._prompt_remove(probe)
        self._poll.start(POLL_INTERVAL_MS)

    def _finish_verification(self, record, probe):
        actual = probe.root, probe.point
        verified = actual == (record.root, record.point)
        if not verified and actual != (record.old_root, record.old_point):
            self._set_pending(record, "断电后读到意外编号。请核对探头并保留记录。", probe=probe)
            return
        note = "断电复核通过，编号已完成。" if verified else "断电后仍为原编号，本次写入失败。"
        if verified and record.advance and record.point == 254:
            note += " 已到点号上限，请选择下一组。"
        try:
            result = self.storage.finish(record.id, verified, note)
        except (ValueError, sqlite3.Error) as error:
            self._publish(phase="pending", busy=False, probe=probe, message=f"编号记录保存失败。{error}")
            return
        self._publish(phase="verified" if verified else "error", pending=None, busy=False, probe=probe, message=note)
        self.records_changed.emit()
        if verified:
            self.completed.emit(result)

    def verified_assignment(self):
        probe = self.state.probe
        return self.storage.verified_assignment(probe.rom_hex) if probe else None

    def verified_number(self, batch):
        probe = self.state.probe
        assignment = self.verified_assignment()
        if (assignment is not None and assignment.batch.casefold() == batch.strip().casefold()
                and (probe.root, probe.point, probe.configuration) == (assignment.root, assignment.point, assignment.configuration)):
            return assignment.root, assignment.point
        return None

    def records(self, batch=None):
        return self.storage.records(batch)

    def batches(self):
        return self.storage.batches()

    def export_csv(self, path, batch=None):
        return self.storage.export_csv(path, batch)

    def demo_remove(self):
        if self.demo and self.state.phase != "writing":
            self.transport.remove()
            if not self.state.busy:
                phase = "disconnected" if not self.state.fixture else "pending" if self.state.pending else "ready"
                self._publish(probe=None, phase=phase, message="模拟探头已拔出。")

    def demo_insert(self, new_probe=False):
        if not self.demo or self.state.phase == "writing" or (new_probe and (self.state.pending or self.state.busy)):
            return
        try:
            self.transport.insert(new_probe)
        except (ValueError, OSError) as error:
            self._publish(message=str(error))
            return
        if not self.state.busy:
            self.read_probe()

    def demo_restore_pending(self):
        if not self.demo or not self.state.fixture or self.state.busy or self.state.pending is None:
            return
        try:
            self.transport.restore(self.state.pending.rom_hex)
        except (ValueError, OSError) as error:
            self._publish(phase="pending", probe=None, message=str(error))
            return
        self.read_probe()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._poll.stop()
        self.transport.close()
        self.storage.close()

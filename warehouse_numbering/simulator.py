"""Numbering demonstration transport with separately persisted virtual EEPROM."""

import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from PySide6.QtCore import QObject, QTimer, Signal

from warehouse_pc.domain import crc8, rom_bytes

from .protocol import CAPABILITY, CAPABILITY_BYTES, LAST_WRITE, WRITE_NUMBER, FixtureError, decode_reply, make_request


def _demo_rom(index):
    body = b"\x2c" + index.to_bytes(6, "little")
    return (body + bytes((crc8(body),))).hex().upper()


def _number(data, name):
    number = data[name]
    if type(number) is not int or not 0 <= number <= 255:
        raise ValueError("编号字节无效")
    return number


class SimulatedFixture(QObject):
    disconnected = Signal(str)

    def __init__(self, data_dir, parent=None):
        super().__init__(parent)
        self.path = Path(data_dir) / "numbering-demo-eeprom.json"
        self._current_rom = _demo_rom(1)
        self._probes = {self._current_rom: (75, 70)}
        self.root, self.point = self._probes[self._current_rom]
        self.present = True
        self.connected = False
        self.write_count = 0
        self.last_write = None
        self._generation = 0
        self._load_error = None
        self._needs_save = True
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "version" not in data:
                index = data["index"]
                if type(index) is not int or not 1 <= index < 1 << 48:
                    raise ValueError("探头序号无效")
                current_rom = _demo_rom(index)
                probes = {current_rom: (_number(data, "root"), _number(data, "point"))}
            else:
                if data["version"] != 1:
                    raise ValueError("库存版本无效")
                current_rom = data["current_rom"]
                probes = {}
                for rom, values in data["probes"].items():
                    raw = rom_bytes(rom)
                    if raw[0] != 0x2C or rom != raw.hex().upper() or not int.from_bytes(raw[1:7], "little"):
                        raise ValueError("库存 ROM 无效")
                    probes[rom] = (_number(values, "root"), _number(values, "point"))
                self._needs_save = False
            root, point = probes[current_rom]
            self._current_rom, self._probes = current_rom, probes
            self.root, self.point = root, point
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            self._load_error = FixtureError(f"无法读取模拟探头库存。{error}")

    @property
    def rom(self):
        return bytes.fromhex(self._current_rom)

    @staticmethod
    def available_ports():
        return ["SIMULATOR"]

    def open(self, _port_name, _baud, _parity, _stop_bits):
        if self._load_error is not None:
            raise self._load_error
        if self._needs_save:
            self._persist(self._probes, self._current_rom)
        self.connected = True

    def close(self):
        self.connected = False
        self._generation += 1

    def remove(self):
        self.present = False

    def insert(self, new_probe=False):
        if new_probe:
            index = max(int.from_bytes(bytes.fromhex(rom)[1:7], "little") for rom in self._probes)
            if index >= (1 << 48) - 1:
                raise FixtureError("模拟探头序号已用完。")
            rom = _demo_rom(index + 1)
            self._persist({**self._probes, rom: (75, 70)}, rom)
        self.present = True

    def restore(self, rom_hex):
        if rom_hex not in self._probes:
            raise FixtureError("模拟库存中没有这只待核验探头。")
        self._persist(self._probes, rom_hex)
        self.present = True

    def _persist(self, probes, current_rom):
        data = {"version": 1, "current_rom": current_rom, "probes": {
            rom: {"root": root, "point": point} for rom, (root, point) in probes.items()
        }}
        temporary = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream)
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._current_rom, self._probes = current_rom, probes
        self.root, self.point = probes[current_rom]
        self._needs_save = False

    def request(self, address, op, payload, token, callback):
        make_request(address, op, payload)
        epoch = self._generation

        def respond():
            if not self.connected or epoch != self._generation:
                return
            try:
                result, data = self._reply(op, payload, token)
                value = decode_reply(op, result, data, token=token)
            except (ValueError, OSError) as error:
                callback(None, error)
            else:
                callback(value, None)

        QTimer.singleShot(30, respond)

    def _reply(self, op, payload, token):
        if op == CAPABILITY:
            return 0, CAPABILITY_BYTES
        if op == LAST_WRITE:
            if self.last_write and self.last_write[0][:4] == token:
                return 0, token + bytes((self.last_write[1],))
            return 7, b""
        if not self.present:
            return 7, b""
        if op == WRITE_NUMBER:
            if self.last_write and self.last_write[0][:4] == token:
                previous, result, data = self.last_write
                return (result, data) if payload == previous else (2, b"")
            if payload[4:15] != self.rom + bytes((self.root, self.point, 0x7F)):
                self.last_write = (payload, 2, b"")
                return 2, b""
            root, point = payload[-2:]
            if (root, point) != (self.root, self.point):
                self._persist({**self._probes, self._current_rom: (root, point)}, self._current_rom)
                self.write_count += 1
        scratch = bytes((0x50, 0x05, self.root, self.point, 0x7F, 0xFF, 0x60, 0x00))
        response = token + self.rom + scratch + bytes((crc8(scratch),))
        if op == WRITE_NUMBER:
            self.last_write = (payload, 0, response)
        return 0, response

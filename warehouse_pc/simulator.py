"""Stateful, isolated fake boards speaking the actual draft wire protocols.

No serial device or network is opened. SerialManager owns one ProtocolSimulator
per profile, so two simulated ports cannot mutate each other's bindings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import time
from typing import Any, Callable

from .domain import crc8, rom_bytes
from .protocol import (
    AMBIENT_BASE, AMBIENT_CAPACITY, BINDING_PAGE_LIMIT, GRAIN_CAPACITY, STATUS_BASE,
    ProtocolError, crc16, encode_humidity, encode_temperature, with_crc,
)


def _u16(value: int) -> bytes:
    return value.to_bytes(2, "big")


def _read16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 2], "big")


def _rom(serial: int) -> bytes:
    body = bytes((0x28,)) + serial.to_bytes(6, "little")
    return body + bytes((crc8(body),))


@dataclass
class _Board:
    address: int
    boot: float
    bindings: dict[tuple[str, int], tuple[int, bytes]] = field(default_factory=dict)
    staged: dict[tuple[str, int], tuple[int, bytes]] = field(default_factory=dict)
    faults: dict[tuple[str, int], int] = field(default_factory=dict)
    generation: int = 1
    staged_revision: int = 1
    dirty: bool = False
    scan_until: float = 0.0
    scan_result: int = 0
    discovered: list[tuple[int, bytes]] = field(default_factory=list)
    not_ready_until: float = 0.0


class ProtocolSimulator:
    """A virtual serial bus with independent boards for each addressed unit.

    ``clock`` is injectable for deterministic scan/timestamp tests. Config is
    retained for this simulator instance, including disconnect/reconnect; it is
    deliberately not persisted to disk or confused with hardware backups.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.boards: dict[int, _Board] = {}

    def board(self, address: int) -> _Board:
        if address not in self.boards:
            board = _Board(address, self.clock())
            serial = 1
            for kind, capacity in (("grain", GRAIN_CAPACITY), ("ambient", AMBIENT_CAPACITY)):
                for point in range(1, capacity + 1):
                    board.bindings[(kind, point)] = (0, _rom(address * 1000 + serial))
                    serial += 1
            board.staged = board.bindings.copy()
            self.boards[address] = board
        return self.boards[address]

    def set_status(self, address: int, kind: str, point: int, status: int) -> None:
        self.board(address).faults[(kind, point)] = status

    def _tick_scan(self, board: _Board) -> None:
        if board.scan_until and self.clock() >= board.scan_until:
            board.scan_until = 0.0
            # Physical probes stay connected when a logical binding is removed.
            board.discovered = [(0, _rom(board.address * 1000 + index))
                                for index in range(1, GRAIN_CAPACITY + AMBIENT_CAPACITY + 1)]
            board.scan_result = 0

    def _sample(self, board: _Board, kind: str, point: int) -> tuple[int, int, int, int]:
        updated_ms = max(0, int((self.clock() - board.boot) * 1000)) // 2000 * 2000
        status = board.faults.get((kind, point), 0)
        if (kind, point) not in board.bindings:
            status = 1
        elif self.clock() < board.not_ready_until:
            status = 2
        if kind == "grain":
            if point == 1:
                temperature = -190
            elif point == 2:
                temperature = -198
            else:
                temperature = 190 + point % 37 + round(6 * math.sin(point / 9 + updated_ms / 20000))
            humidity = 480 + point % 80
        else:
            temperature, humidity = 180 + point * 9, 500 + point * 13
        return temperature, humidity, status, updated_ms & 0xFFFFFFFF

    def _temperature(self, board: _Board, kind: str, point: int, negative_offset: int) -> bytes:
        temperature, _, status, _ = self._sample(board, kind, point)
        return _u16(0xFFFF if status else encode_temperature(temperature, negative_offset))

    def _humidity(self, board: _Board, point: int) -> bytes:
        _, humidity, status, _ = self._sample(board, "ambient", point)
        return _u16(0xFFFF if status else encode_humidity(humidity))

    def _register(self, board: _Board, address: int) -> int:
        if 1 <= address <= GRAIN_CAPACITY:
            return int.from_bytes(self._temperature(board, "grain", address, 0x10), "big")
        if address == 0x100:
            return 1
        if AMBIENT_BASE <= address < AMBIENT_BASE + AMBIENT_CAPACITY * 2:
            point = (address - AMBIENT_BASE) // 2 + 1
            if (address - AMBIENT_BASE) % 2:
                return int.from_bytes(self._humidity(board, point), "big")
            return int.from_bytes(self._temperature(board, "ambient", point, 0x10), "big")
        if STATUS_BASE <= address < STATUS_BASE + GRAIN_CAPACITY + AMBIENT_CAPACITY:
            index = address - STATUS_BASE
            kind, point = ("grain", index + 1) if index < GRAIN_CAPACITY else ("ambient", index - GRAIN_CAPACITY + 1)
            return self._sample(board, kind, point)[2]
        raise KeyError(address)

    def _lc(self, request: bytes) -> bytes:
        if len(request) != 22 or request[0] != 0xF7 or request[2] == 0:
            return b""
        board, command = self.board(request[2]), request[3]
        start, count, channel = request[4:7]
        reply = bytearray((0xF8, 0x8F, board.address, command, 0, 0))
        if command in (0xD0, 0xD1):
            if not start or not count or start + count - 1 > GRAIN_CAPACITY or channel > AMBIENT_CAPACITY:
                return b""
            if command == 0xD0:
                for point in range(start, start + count):
                    reply.extend(self._temperature(board, "grain", point, 10))
                if channel:
                    reply.extend(self._temperature(board, "ambient", channel, 10))
                reply.extend(self._humidity(board, channel or 5))
            else:
                if not channel or not 1 <= request[7] <= 8:
                    return b""
                reply[4:6] = bytes((request[7], start))
                temperatures = [self._sample(board, "grain", point) for point in range(start, start + count)]
                valid = [sample[0] for sample in temperatures if sample[2] == 0]
                reply.extend(_u16(encode_temperature(max(valid), 10) if valid else 0xFFFF))
                reply.extend(self._temperature(board, "ambient", channel, 10))
                reply.extend(self._humidity(board, channel))
        elif command == 0xD2:
            if not 1 <= start <= AMBIENT_CAPACITY:
                return b""
            reply = reply[:4] + bytes((start,))
            reply.extend(self._temperature(board, "ambient", start, 10))
            reply.extend(self._humidity(board, start))
        elif command == 0xD8:
            span = max((point for kind, point in board.bindings if kind == "grain"), default=0)
            reply = reply[:4] + bytes((span,)) * 4
        elif command == 0xD9:
            reply = reply[:4] + b"\x67" * 4
        elif command not in (0xD3, 0xD4, 0xDA, 0xDD):
            return b""
        return bytes(reply) + b"\x00\xFD\xDF"

    @staticmethod
    def _kind_point(payload: bytes) -> tuple[str, int] | None:
        if len(payload) < 3:
            return None
        kind = {1: "grain", 2: "ambient"}.get(payload[0])
        point = _read16(payload, 1)
        if kind is None or not 1 <= point <= (GRAIN_CAPACITY if kind == "grain" else AMBIENT_CAPACITY):
            return None
        return kind, point

    def _maintenance(self, board: _Board, op: int, payload: bytes) -> tuple[int, bytes]:
        self._tick_scan(board)
        busy = bool(board.scan_until)
        if op == 0 and not payload:
            info = bytes((1, 1, int(board.dirty), int(busy)))
            info += _u16(len(board.bindings)) + _u16(len(board.discovered))
            info += board.generation.to_bytes(4, "big") + _u16(GRAIN_CAPACITY) + _u16(AMBIENT_CAPACITY)
            return 0, info + bytes((1, board.scan_result))
        if op == 1 and len(payload) == 1:
            if payload[0] != 0:
                return 1, b""
            if busy:
                return 4, b""
            board.discovered = []
            board.scan_until = self.clock() + 0.2
            board.scan_result = 4
            return 0, b""
        if op == 2 and len(payload) == 3:
            if board.scan_result:
                return board.scan_result, b""
            offset, count = _read16(payload, 0), payload[2]
            if not 1 <= count <= 27 or offset > len(board.discovered):
                return 1, b""
            records = board.discovered[offset:offset + count]
            data = _u16(len(board.discovered)) + _u16(offset) + bytes((len(records),))
            return 0, data + b"".join(bytes((bus,)) + rom for bus, rom in records)
        if op in (3, 4, 5, 6) and busy:
            return 4, b""
        if op == 3 and len(payload) == 12:
            key = self._kind_point(payload)
            if key is None or payload[3] != 0:
                return 1, b""
            try:
                rom = rom_bytes(payload[4:12])
            except ValueError:
                return 2, b""
            if any(old_key != key and binding[1] == rom for old_key, binding in board.staged.items()):
                return 3, b""
            binding = (payload[3], rom)
            if board.staged.get(key) != binding:
                board.staged[key] = binding
                board.dirty = True
                board.staged_revision = (board.staged_revision + 1) & 0xFFFFFFFF
            return 0, b""
        if op in (4, 7, 8):
            required = 4 if op == 7 else 3
            if len(payload) != required:
                return 2, b""
            key = self._kind_point(payload)
            if key is None:
                return 1, b""
            if op == 4:
                if key not in board.staged:
                    return 7, b""
                del board.staged[key]
                board.dirty = True
                board.staged_revision = (board.staged_revision + 1) & 0xFFFFFFFF
                return 0, b""
            if op == 7:
                table = payload[3]
                if table not in (0, 1):
                    return 2, b""
                binding = (board.staged if table else board.bindings).get(key)
                if binding is None:
                    return 7, b""
                return 0, payload + bytes((binding[0],)) + binding[1]
            temperature, humidity, status, updated_ms = self._sample(board, *key)
            return 0, payload + bytes((status,)) + temperature.to_bytes(2, "big", signed=True) + _u16(humidity) + updated_ms.to_bytes(4, "big")
        if op == 5 and not payload:
            if board.dirty:
                board.bindings = board.staged.copy()
                board.generation = (board.generation + 1) & 0xFFFFFFFF
                board.staged_revision = (board.staged_revision + 1) & 0xFFFFFFFF
                board.dirty = False
                board.not_ready_until = self.clock() + 0.1
            return 0, b""
        if op == 6 and not payload:
            board.staged = board.bindings.copy()
            board.dirty = False
            board.staged_revision = (board.staged_revision + 1) & 0xFFFFFFFF
            board.not_ready_until = self.clock() + 0.1
            return 0, b""
        if op == 9 and len(payload) == 4:
            table, offset, limit = payload[0], _read16(payload, 1), payload[3]
            if table not in (0, 1):
                return 2, b""
            bindings = board.staged if table else board.bindings
            if not 1 <= limit <= BINDING_PAGE_LIMIT or offset > len(bindings):
                return 1, b""
            revision = board.staged_revision if table else board.generation
            records = list(bindings.items())[offset:offset + limit]
            header = bytes((table,)) + _u16(len(bindings)) + _u16(offset) + bytes((len(records),))
            data = b"".join(
                bytes((1 if kind == "grain" else 2,)) + _u16(point) + bytes((bus,)) + rom
                for (kind, point), (bus, rom) in records
            )
            return 0, header + revision.to_bytes(4, "big") + data
        return 2, b""

    def simulate_request(self, request: bytes, expected: dict[str, Any] | None = None) -> bytes:
        request = bytes(request)
        # Test actual wire bytes, not the GUI's expected result.
        if len(request) == 22 and request[0] == 0xF7:
            # Same conservative ambiguity rule as the firmware.
            if crc16(request[:-2]) == int.from_bytes(request[-2:], "little"):
                return b""
            return self._lc(request)
        if not 4 <= len(request) <= 256 or not 1 <= request[0] <= 247:
            return b""
        if crc16(request[:-2]) != int.from_bytes(request[-2:], "little"):
            return b""
        board = self.board(request[0])
        function = request[1]
        if function == 3:
            if len(request) != 8:
                return with_crc(bytes((board.address, 0x83, 3)))
            start, count = _read16(request, 2), _read16(request, 4)
            if not 1 <= count <= 125:
                return with_crc(bytes((board.address, 0x83, 3)))
            if start + count > 65536:
                return with_crc(bytes((board.address, 0x83, 2)))
            try:
                registers = [self._register(board, register) for register in range(start, start + count)]
            except KeyError:
                return with_crc(bytes((board.address, 0x83, 2)))
            return with_crc(bytes((board.address, 3, count * 2)) + b"".join(_u16(value) for value in registers))
        if function == 0x41:
            if len(request) < 5:
                return with_crc(bytes((board.address, 0xC1, 3)))
            result, data = self._maintenance(board, request[2], request[3:-2])
            return with_crc(bytes((board.address, 0x41, request[2], result)) + data)
        return with_crc(bytes((board.address, function | 0x80, 1)))


_simulated_profiles: dict[str, ProtocolSimulator] = {}


def simulate_request(request: bytes, expected: dict[str, Any] | None = None,
                     profile_id: str = "default") -> bytes:
    """Convenience entry point; profile states remain strictly independent."""
    simulator = _simulated_profiles.setdefault(profile_id, ProtocolSimulator())
    return simulator.simulate_request(request, expected)

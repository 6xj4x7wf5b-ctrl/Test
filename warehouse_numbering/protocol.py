"""Wire codec for the STM32 numbering fixture's private maintenance functions."""

from dataclasses import dataclass

from warehouse_pc.domain import crc8, rom_bytes
from warehouse_pc.protocol import crc16, with_crc

FUNCTION = 0x41
CAPABILITY, READ_SINGLE, WRITE_NUMBER, LAST_WRITE = 0x20, 0x21, 0x22, 0x23
CAPABILITY_BYTES = b"\x01\x01\x07\x01\x01\xfe"
SUCCESS_LENGTH = {CAPABILITY: 6, READ_SINGLE: 21, WRITE_NUMBER: 21, LAST_WRITE: 5}
REQUEST_LENGTH = {CAPABILITY: 0, READ_SINGLE: 4, WRITE_NUMBER: 17, LAST_WRITE: 4}


class FixtureError(ValueError):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result


RESULT_MESSAGES = {
    1: "编号超出工装支持范围。",
    2: "工装拒绝请求。请核对原编号、探头和工装版本。",
    3: "检测到多只探头，请只接入一只。",
    4: "工装忙，请稍后读取。",
    5: "工装保存失败。",
    6: "探头通信或 CRC 校验失败。",
    7: "工装未检测到探头。",
}


@dataclass(frozen=True, slots=True)
class ProbeReading:
    rom: bytes
    root: int
    point: int
    configuration: int
    scratch: bytes

    @property
    def rom_hex(self):
        return self.rom.hex().upper()


def make_request(address, op, payload=b""):
    if not 1 <= address <= 247 or op not in REQUEST_LENGTH or len(payload) != REQUEST_LENGTH[op]:
        raise FixtureError("无效的工装请求。")
    return with_crc(bytes((address, FUNCTION, op)) + payload)


def write_payload(token, probe, root, point):
    if any(type(value) is not int or not 1 <= value <= 254 for value in (root, point)):
        raise FixtureError("根号和点号必须在 1～254 范围内。")
    return token + probe.rom + bytes((probe.root, probe.point, probe.configuration, root, point))


def decode_reply(op, result, data, token=None):
    if result:
        raise FixtureError(RESULT_MESSAGES.get(result, f"工装错误 {result}。"), result)
    if op == CAPABILITY:
        if data != CAPABILITY_BYTES:
            raise FixtureError("此设备未声明受支持的 GXHT3W 编号工装能力。")
        return None
    if len(data) != SUCCESS_LENGTH.get(op) or data[:4] != token:
        raise FixtureError("工装响应与请求不匹配。")
    if op == LAST_WRITE:
        return data[4]
    rom = rom_bytes(data[4:12])
    scratch = data[12:]
    if scratch in (bytes(9), b"\xff" * 9) or crc8(scratch[:8]) != scratch[8]:
        raise FixtureError("探头暂存区 CRC 校验失败。", 6)
    return ProbeReading(rom, scratch[2], scratch[3], scratch[4], scratch)


class ReplyBuffer:
    """Find a matching complete frame while retaining incomplete serial reads."""

    def __init__(self, address, op, token=None):
        self.address, self.op, self.token = address, op, token
        self.buffer = bytearray()
        self.bad_crc = False

    def feed(self, data):
        self.buffer.extend(data)
        incomplete = None
        for start in range(len(self.buffer) - 1):
            if self.buffer[start] != self.address:
                continue
            function = self.buffer[start + 1]
            remaining = len(self.buffer) - start
            if function == (FUNCTION | 0x80):
                length = 5
            elif function == FUNCTION:
                if remaining < 4:
                    incomplete = start if incomplete is None else incomplete
                    continue
                if self.buffer[start + 2] != self.op:
                    continue
                length = 6 + (SUCCESS_LENGTH[self.op] if self.buffer[start + 3] == 0 else 0)
            else:
                continue
            if remaining < length:
                incomplete = start if incomplete is None else incomplete
                continue
            frame = bytes(self.buffer[start:start + length])
            if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
                self.bad_crc = True
                continue
            if function != FUNCTION:
                self.buffer.clear()
                raise FixtureError(f"工装返回 Modbus 异常 {frame[2]:02X}。")
            result, payload = frame[3], frame[4:-2]
            if not result and self.token is not None and payload[:4] != self.token:
                continue
            self.buffer.clear()
            return result, payload
        keep = incomplete if incomplete is not None else max(0, len(self.buffer) - 1)
        del self.buffer[:keep]
        return None

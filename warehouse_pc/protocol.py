"""Pure-Python codecs for the current LC-M3/Modbus firmware draft.

Builders return whole requests. Parsers return dictionaries; all carry a
``protocol`` and ``address`` key. Optional parser context may contain the whole
transport ``expected`` dictionary: unknown keys are deliberately ignored.
LC request byte 1 and its unused bytes are zero; their real legacy meanings
are still unconfirmed. Only use this layout with the documented firmware.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .domain import crc8 as crc8, rom_bytes

GRAIN_CAPACITY = 165
AMBIENT_CAPACITY = 5
BINDING_PAGE_LIMIT = 20
AMBIENT_BASE = 0x2000
STATUS_BASE = 0x4000
STATUS_UNAVAILABLE = 255  # FFFF does not identify the underlying sensor fault.
STATUS_TEXT = {
    0: "正常", 1: "未绑定", 2: "尚未采样", 3: "离线", 4: "CRC错误",
    5: "总线故障", 6: "数据过期", 7: "供电模式错误", 8: "转换超时",
    9: "数据格式无效", 255: "无有效数据",
}
RESULT_TEXT = {
    0: "成功", 1: "范围错误", 2: "无效参数或操作", 3: "重复绑定",
    4: "设备忙", 5: "存储错误", 6: "I/O错误", 7: "未找到",
}


class ProtocolError(ValueError):
    """A malformed frame, wrong device, or inconsistent request context."""


class DeviceException(ProtocolError):
    def __init__(self, address: int, function: int, code: int):
        self.address, self.function, self.code = address, function, code
        names = {1: "不支持的功能", 2: "寄存器地址无效", 3: "参数无效", 4: "设备故障"}
        super().__init__(f"分机 {address}：{names.get(code, 'Modbus异常')}（{code:02X}）")


@dataclass(frozen=True, slots=True)
class Sample:
    kind: str
    point: int
    temperature: float | None
    humidity: float | None = None
    status: int = 0


def _integer(value: int, minimum: int, maximum: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ProtocolError(f"{name} 必须为 {minimum}～{maximum} 的整数")
    return value


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def with_crc(data: bytes) -> bytes:
    return bytes(data) + crc16(data).to_bytes(2, "little")


def validate_crc(frame: bytes) -> None:
    if not 4 <= len(frame) <= 256:
        raise ProtocolError("RTU帧长度无效")
    if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        raise ProtocolError("RTU CRC校验失败")


def build_lc_request(address: int, command: int, payload: bytes = b"") -> bytes:
    _integer(address, 1, 255, "分机地址")
    _integer(command, 0, 255, "命令")
    payload = bytes(payload)
    if len(payload) > 18:
        raise ProtocolError("LC-M3参数最多18字节")
    return bytes((0xF7, 0, address, command)) + payload + bytes(18 - len(payload))


def build_modbus_read(address: int, start: int, count: int) -> bytes:
    _integer(address, 1, 247, "Modbus地址")
    _integer(start, 0, 65535, "寄存器地址")
    _integer(count, 1, 125, "寄存器数量")
    if start + count > 65536:
        raise ProtocolError("寄存器范围越界")
    return with_crc(bytes((address, 3)) + start.to_bytes(2, "big") + count.to_bytes(2, "big"))


def _maintenance_rom(data: bytes) -> bytes:
    try:
        return rom_bytes(data)
    except ValueError as exc:
        raise ProtocolError(str(exc)) from exc


def build_maintenance(address: int, op: int, payload: bytes = b"") -> bytes:
    _integer(address, 1, 247, "维护地址")
    _integer(op, 0, 255, "维护操作码")
    payload = bytes(payload)
    if len(payload) > 251:
        raise ProtocolError("维护参数过长")
    if op == 3:
        _maintenance_rom(payload[4:])
    elif op == 9 and (
        len(payload) != 4 or payload[0] not in (0, 1)
        or _word(payload, 1) > GRAIN_CAPACITY + AMBIENT_CAPACITY
        or not 1 <= payload[3] <= BINDING_PAGE_LIMIT
    ):
        raise ProtocolError("绑定分页请求的表、偏移或条数无效")
    return with_crc(bytes((address, 0x41, op)) + payload)


def decode_temperature(value: int, negative_offset: int) -> float | None:
    if value == 0xFFFF:
        return None
    whole, fraction = value >> 8, value & 255
    negative = fraction >= negative_offset
    decimal = fraction - negative_offset if negative else fraction
    if not 0 <= decimal <= 9:
        raise ProtocolError("温度小数字节无效")
    result = whole + decimal / 10
    return -result if negative else result


def decode_humidity(value: int) -> float | None:
    if value == 0xFFFF:
        return None
    whole, fraction = value >> 8, value & 255
    if fraction > 9 or whole > 100 or (whole == 100 and fraction):
        raise ProtocolError("湿度编码超出0～100%RH")
    return whole + fraction / 10


def encode_temperature(value_x10: int, negative_offset: int) -> int:
    """Simulator/test helper, not a calibration operation."""
    magnitude = abs(int(value_x10))
    if magnitude > 2559:
        raise ProtocolError("温度超出协议范围")
    return (magnitude // 10 << 8) | (magnitude % 10 + (negative_offset if value_x10 < 0 else 0))


def encode_humidity(value_x10: int) -> int:
    _integer(value_x10, 0, 1000, "湿度")
    return value_x10 // 10 << 8 | value_x10 % 10


def _word(data: bytes, index: int) -> int:
    return int.from_bytes(data[index:index + 2], "big")


def _sample(kind: str, point: int, temperature: float | None,
            humidity: float | None = None, *, require_humidity: bool = False) -> Sample:
    missing = temperature is None or (require_humidity and humidity is None)
    return Sample(kind, point, temperature, humidity, STATUS_UNAVAILABLE if missing else 0)


def lc_response_length(command: int, **context: Any) -> int:
    if command == 0xD0:
        count = _integer(context.get("count", 0), 1, GRAIN_CAPACITY, "粮温点数")
        channel = context.get("ambient_channel", context.get("ambient", 1))
        _integer(channel, 0, AMBIENT_CAPACITY, "仓温湿通道")
        return 2 * count + (13 if channel else 11)
    lengths = {0xD1: 15, 0xD2: 12, 0xD8: 11, 0xD9: 11,
               0xD3: 9, 0xD4: 9, 0xDA: 9, 0xDD: 9}
    if command not in lengths:
        raise ProtocolError(f"未支持的LC-M3命令 {command:02X}")
    return lengths[command]


def parse_lc_response(frame: bytes, command: int, **context: Any) -> dict[str, Any]:
    frame = bytes(frame)
    expected_length = lc_response_length(command, **context)
    if len(frame) != expected_length or frame[:2] != b"\xF8\x8F" or frame[-3:] != b"\x00\xFD\xDF":
        raise ProtocolError("LC-M3帧头、长度或帧尾错误")
    if frame[3] != command:
        raise ProtocolError("LC-M3响应命令不匹配")
    if "address" in context and frame[2] != context["address"]:
        raise ProtocolError("LC-M3响应地址不匹配")
    result: dict[str, Any] = {"protocol": "lc", "address": frame[2], "command": command, "samples": []}
    samples: list[Sample] = result["samples"]
    if command == 0xD0:
        count, start = context["count"], context.get("start", 1)
        if not 1 <= start <= GRAIN_CAPACITY or start + count - 1 > GRAIN_CAPACITY:
            raise ProtocolError("LC-M3查询测点范围无效")
        for index in range(count):
            samples.append(_sample("grain", start + index, decode_temperature(_word(frame, 6 + index * 2), 10)))
        index = 6 + count * 2
        channel = context.get("ambient_channel", context.get("ambient", 1))
        if channel:
            samples.append(_sample("ambient", channel, decode_temperature(_word(frame, index), 10),
                                   decode_humidity(_word(frame, index + 2)), require_humidity=True))
        else:
            # Channel-zero returns humidity only; do not invent an air temperature.
            result["ambient_humidity"] = {"point": 5, "humidity": decode_humidity(_word(frame, index))}
    elif command == 0xD2:
        point = frame[4]
        expected_point = context.get("point", context.get("start", point))
        if not 1 <= point <= AMBIENT_CAPACITY or point != expected_point:
            raise ProtocolError("仓温湿通道不匹配")
        samples.append(_sample("ambient", point, decode_temperature(_word(frame, 5), 10),
                               decode_humidity(_word(frame, 7)), require_humidity=True))
    elif command == 0xD1:
        result["maximum"] = decode_temperature(_word(frame, 6), 10)
        result["start"] = frame[5]
        result["fan_channel"] = frame[4]  # Echo only; this command never controls a fan.
        point = context.get("ambient_channel", context.get("ambient", 1))
        samples.append(_sample("ambient", point, decode_temperature(_word(frame, 8), 10),
                               decode_humidity(_word(frame, 10)), require_humidity=True))
    elif command == 0xD8:
        if len(set(frame[4:8])) != 1 or frame[4] > GRAIN_CAPACITY:
            raise ProtocolError("LC-M3测点跨度返回值无效")
        result["grain_span"] = frame[4]
    elif command == 0xD9:
        if frame[4:8] != b"\x67" * 4:
            raise ProtocolError("LC-M3握手返回值无效")
        result["online"] = True
    return result


def _validate_modbus_response(frame: bytes, function: int, address: int | None = None) -> None:
    validate_crc(frame)
    if not 1 <= frame[0] <= 247 or (address is not None and frame[0] != address):
        raise ProtocolError("响应分机地址不匹配")
    if frame[1] == function | 0x80:
        if len(frame) != 5:
            raise ProtocolError("Modbus异常帧长度无效")
        raise DeviceException(frame[0], function, frame[2])
    if frame[1] != function:
        raise ProtocolError("响应功能码不匹配")


def decode_registers(start: int, registers: list[int], kind: str | None = None) -> dict[str, Any]:
    """Decode the documented address map; status replies never invent values."""
    samples: list[Sample] = []
    statuses: list[dict[str, Any]] = []
    for index, value in enumerate(registers):
        address = start + index
        if 1 <= address <= GRAIN_CAPACITY:
            samples.append(_sample("grain", address, decode_temperature(value, 0x10)))
        elif AMBIENT_BASE <= address < AMBIENT_BASE + AMBIENT_CAPACITY * 2:
            # Assemble temperature/humidity only when both are present in this reply.
            if (address - AMBIENT_BASE) % 2:
                if index == 0:
                    raise ProtocolError("仓温湿批读须从温度寄存器开始")
                continue
            if index + 1 >= len(registers):
                raise ProtocolError("仓温湿批读缺少湿度寄存器")
            samples.append(_sample("ambient", (address - AMBIENT_BASE) // 2 + 1,
                                   decode_temperature(value, 0x10), decode_humidity(registers[index + 1]),
                                   require_humidity=True))
        elif STATUS_BASE <= address < STATUS_BASE + GRAIN_CAPACITY + AMBIENT_CAPACITY:
            number = address - STATUS_BASE
            statuses.append({"kind": "grain" if number < GRAIN_CAPACITY else "ambient",
                             "point": number + 1 if number < GRAIN_CAPACITY else number - GRAIN_CAPACITY + 1,
                             "status": value})
        elif address != 0x100:
            raise ProtocolError("响应包含未定义寄存器")
    return {"samples": samples, "statuses": statuses}


def parse_modbus_response(frame: bytes, **context: Any) -> dict[str, Any]:
    frame = bytes(frame)
    _validate_modbus_response(frame, 3, context.get("address"))
    if len(frame) < 5 or not frame[2] or frame[2] > 250 or frame[2] % 2 or len(frame) != frame[2] + 5:
        raise ProtocolError("Modbus字节数量或响应长度无效")
    registers = [_word(frame, offset) for offset in range(3, len(frame) - 2, 2)]
    if "count" in context and len(registers) != context["count"]:
        raise ProtocolError("寄存器响应数量与请求不一致")
    result: dict[str, Any] = {"protocol": "modbus", "address": frame[0], "function": 3,
                              "registers": registers}
    if "start" in context:
        result["start"] = context["start"]
        result.update(decode_registers(context["start"], registers, context.get("kind")))
    return result


def _binding_page_header(data: bytes, context: dict[str, Any]) -> tuple[int, int, int, int]:
    table, total, offset, count = data[0], _word(data, 1), _word(data, 3), data[5]
    if (table not in (0, 1) or total > GRAIN_CAPACITY + AMBIENT_CAPACITY or offset > total
            or count > BINDING_PAGE_LIMIT or offset + count > total
            or (count == 0 and offset < total)):
        raise ProtocolError("绑定分页的表、偏移或条数无效")
    if table != context.get("table", table) or offset != context.get("offset", offset):
        raise ProtocolError("绑定分页与请求的表或偏移不匹配")
    if "limit" in context and count != min(context["limit"], total - offset):
        raise ProtocolError("绑定分页返回的条数与请求不匹配")
    return table, total, offset, count


def parse_maintenance(frame: bytes, **context: Any) -> dict[str, Any]:
    frame = bytes(frame)
    _validate_modbus_response(frame, 0x41, context.get("address"))
    if len(frame) < 6:
        raise ProtocolError("维护响应过短")
    op, code, data = frame[2], frame[3], frame[4:-2]
    if "op" in context and op != context["op"]:
        raise ProtocolError("维护响应操作码不匹配")
    result: dict[str, Any] = {"protocol": "maintenance", "address": frame[0], "op": op,
                              "result": code, "data": data}
    if code:
        if data:
            raise ProtocolError("错误维护响应不应含成功数据")
        return result
    lengths = {0: 18, 1: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 13, 8: 12}
    if op in lengths and len(data) != lengths[op]:
        raise ProtocolError("维护响应数据长度无效")
    if op == 0:
        result["info"] = {
            "version": data[0], "config_valid": bool(data[1]), "dirty": bool(data[2]),
            "scan_busy": bool(data[3]), "active_count": _word(data, 4),
            "discovered_count": _word(data, 6), "generation": int.from_bytes(data[8:12], "big"),
            "grain_capacity": _word(data, 12), "ambient_capacity": _word(data, 14),
            "bus_count": data[16], "scan_status": data[17],
        }
    elif op == 2:
        if len(data) < 5 or data[4] > 27 or len(data) != 5 + 9 * data[4]:
            raise ProtocolError("扫描分页长度无效")
        total, offset = _word(data, 0), _word(data, 2)
        if offset + data[4] > total:
            raise ProtocolError("扫描分页超过总数")
        records = []
        for index in range(data[4]):
            bus = data[5 + index * 9]
            rom = _maintenance_rom(data[6 + index * 9:14 + index * 9])
            records.append({"bus": bus, "rom": rom, "rom_hex": rom.hex().upper()})
        result.update(total=total, offset=offset, records=records)
    elif op == 7:
        kind = {1: "grain", 2: "ambient"}.get(data[0])
        if kind is None or data[3] > 1:
            raise ProtocolError("绑定响应类型或表无效")
        rom = _maintenance_rom(data[5:13])
        result["binding"] = {"kind": kind, "point": _word(data, 1), "table": data[3],
                               "bus": data[4], "rom": rom, "rom_hex": rom.hex().upper()}
    elif op == 8:
        kind = {1: "grain", 2: "ambient"}.get(data[0])
        if kind is None:
            raise ProtocolError("样本测点类型无效")
        status = data[3]
        result["sample"] = Sample(kind, _word(data, 1),
            int.from_bytes(data[4:6], "big", signed=True) / 10 if status == 0 else None,
            _word(data, 6) / 10 if status == 0 else None, status)
        result["updated_ms"] = int.from_bytes(data[8:12], "big")
    elif op == 9:
        if len(data) < 10:
            raise ProtocolError("绑定分页响应过短")
        table, total, offset, count = _binding_page_header(data, context)
        if len(data) != 10 + 12 * count:
            raise ProtocolError("绑定分页响应长度无效")
        records = []
        points, roms = set(), set()
        for index in range(10, len(data), 12):
            kind = {1: "grain", 2: "ambient"}.get(data[index])
            point, bus = _word(data, index + 1), data[index + 3]
            capacity = GRAIN_CAPACITY if kind == "grain" else AMBIENT_CAPACITY
            if kind is None or not 1 <= point <= capacity or bus >= context.get("bus_count", 256):
                raise ProtocolError("绑定分页的测点类型、编号或支路无效")
            rom = _maintenance_rom(data[index + 4:index + 12])
            if (kind, point) in points or rom in roms:
                raise ProtocolError("绑定分页包含重复测点或 ROM")
            points.add((kind, point))
            roms.add(rom)
            records.append({"kind": kind, "point": point, "bus": bus,
                            "rom": rom, "rom_hex": rom.hex().upper()})
        result.update(table=table, total=total, offset=offset,
                      revision=int.from_bytes(data[6:10], "big"), records=records)
    elif op not in lengths:
        raise ProtocolError("未定义的维护成功响应")
    return result


def response_length(buffer: bytes, expected: dict[str, Any]) -> int | None:
    """Length of an addressed response prefix, or None until it is known.

    This does not validate CRC: the transport must parse before accepting it.
    A nonmatching prefix raises ProtocolError so a stream can resynchronize.
    """
    protocol = expected["protocol"]
    if protocol == "lc":
        if buffer and buffer[0] != 0xF8 or len(buffer) >= 2 and buffer[1] != 0x8F:
            raise ProtocolError("不是LC帧头")
        if len(buffer) >= 3 and buffer[2] != expected["address"]:
            raise ProtocolError("非本机响应")
        if len(buffer) >= 4 and buffer[3] != expected["command"]:
            raise ProtocolError("LC命令不匹配")
        return lc_response_length(expected["command"], **{k: v for k, v in expected.items() if k != "command"})
    if protocol not in ("modbus", "maintenance"):
        raise ProtocolError("未知通信协议")
    if buffer and buffer[0] != expected["address"]:
        raise ProtocolError("非本机响应")
    if len(buffer) < 2:
        return None
    function = 3 if protocol == "modbus" else 0x41
    if buffer[1] == function | 0x80:
        return 5
    if buffer[1] != function:
        raise ProtocolError("功能码不匹配")
    if len(buffer) < 3:
        return None
    if protocol == "modbus":
        if not buffer[2] or buffer[2] > 250 or buffer[2] % 2:
            raise ProtocolError("字节数无效")
        if "count" in expected and buffer[2] != expected["count"] * 2:
            raise ProtocolError("字节数与在途请求不匹配")
        return buffer[2] + 5
    if buffer[2] != expected["op"]:
        raise ProtocolError("维护操作码不匹配")
    if len(buffer) < 4:
        return None
    if buffer[3]:
        return 6
    op = buffer[2]
    if op == 2:
        if len(buffer) >= 8 and "offset" in expected and _word(buffer, 6) != expected["offset"]:
            raise ProtocolError("扫描分页位置不匹配")
        if len(buffer) < 9:
            return None
        if buffer[8] > 27:
            raise ProtocolError("扫描页容量无效")
        if "scan_count" in expected and buffer[8] > expected["scan_count"]:
            raise ProtocolError("扫描返回条数超过请求")
        return 11 + 9 * buffer[8]
    if op == 9:
        if len(buffer) < 10:
            return None
        _, _, _, count = _binding_page_header(buffer[4:10], expected)
        return 16 + 12 * count
    lengths = {0: 24, 1: 6, 3: 6, 4: 6, 5: 6, 6: 6, 7: 19, 8: 18}
    if op not in lengths:
        raise ProtocolError("未知维护操作")
    return lengths[op]


def parse_response(frame: bytes, expected: dict[str, Any]) -> dict[str, Any]:
    """Transport adapter accepting its entire expected dictionary."""
    protocol = expected["protocol"]
    if protocol == "lc":
        context = {key: value for key, value in expected.items() if key != "command"}
        return parse_lc_response(frame, expected["command"], **context)
    if protocol == "modbus":
        return parse_modbus_response(frame, **expected)
    if protocol == "maintenance":
        result = parse_maintenance(frame, **expected)
        if result["result"] == 0 and result["op"] == 7:
            binding = result["binding"]
            for key in ("kind", "point", "table"):
                if key in expected and binding[key] != expected[key]:
                    raise ProtocolError("绑定响应与请求测点不一致")
        if result["result"] == 0 and result["op"] == 8:
            for key in ("kind", "point"):
                if key in expected and getattr(result["sample"], key) != expected[key]:
                    raise ProtocolError("样本响应与请求测点不一致")
        if result["result"] == 0 and result["op"] == 2 and "offset" in expected:
            if result["offset"] != expected["offset"]:
                raise ProtocolError("扫描响应分页位置不匹配")
        return result
    raise ProtocolError("未知通信协议")

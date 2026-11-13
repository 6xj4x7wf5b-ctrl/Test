"""Validation and portable report exports; no GUI or hardware dependencies."""
from __future__ import annotations

import csv
import math
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence

KINDS = {"grain", "ambient"}
STATUS_NAMES = {
    0: "ok", 1: "unbound", 2: "not_ready", 3: "offline", 4: "crc_error",
    5: "bus_error", 6: "stale", 7: "power_error", 8: "timeout", 9: "data_error",
}
HISTORY_COLUMNS = (
    "timestamp", "source", "device_id", "device_name", "warehouse_name", "kind", "point",
    "raw_temp", "raw_humidity", "temp", "humidity", "status", "calibration_version",
    "binding_version", "temp_offset", "humidity_offset", "environment_role",
)


def normalize_kind(kind: str) -> str:
    value = str(kind).strip().lower()
    if value not in KINDS:
        raise ValueError("测点类型应为 grain 或 ambient")
    return value


def normalize_status(status: object) -> str:
    if isinstance(status, int) and not isinstance(status, bool):
        return STATUS_NAMES.get(status, f"unknown_{status}")
    value = str(status).strip().lower()
    return {"normal": "ok", "valid": "ok", "0": "ok", "expired": "stale"}.get(value, value or "unknown")


def finite_number(value: object, name: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是有限数字")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} 必须是有限数字") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} 必须是有限数字")
    return number


def positive_int(value: object, name: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} 必须是正整数")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} 超过上限 {maximum}")
    return value


def iso_timestamp(value: str | datetime | None = None) -> str:
    if value is None:
        dt = datetime.now()
    elif isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError("时间必须是 ISO 日期时间") from exc
        if "T" not in value and " " not in value:
            raise ValueError("时间必须包含时分秒")
    else:
        raise ValueError("时间必须是 ISO 日期时间")
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")


def time_range(start: str | None, end: str | None) -> tuple[str | None, str | None]:
    first = iso_timestamp(start) if start is not None else None
    last = iso_timestamp(end) if end is not None else None
    if first is not None and last is not None and first > last:
        raise ValueError("开始时间不能晚于结束时间")
    return first, last


def source_for_port(port_name: str) -> str:
    name = port_name.strip().lower()
    return "simulator" if name in {"sim", "simulator"} or name.startswith(("sim://", "simulator://")) else "serial"


def normalize_source(source: str) -> str:
    value = str(source).strip().lower()
    value = {"sim": "simulator", "mock": "simulator", "simulated": "simulator"}.get(value, value)
    if value not in {"serial", "simulator"}:
        raise ValueError("数据源应为 serial 或 simulator")
    return value


def crc8(data: bytes) -> int:
    """Return the reflected Dallas/Maxim CRC8 used in probe ROMs."""
    crc = 0
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0x8C if crc & 1 else 0)
    return crc


def rom_bytes(value: object) -> bytes:
    """Validate and normalize a complete probe ROM in wire byte order."""
    if isinstance(value, str):
        try:
            data = bytes.fromhex(value.replace("-", "").replace(":", "").replace(" ", ""))
        except ValueError as exc:
            raise ValueError("ROM 编码必须是 16 位十六进制字符") from exc
    elif isinstance(value, (bytes, bytearray)):
        data = bytes(value)
    else:
        raise ValueError("ROM 编码必须是字节或十六进制文本")
    if len(data) != 8:
        raise ValueError("ROM 必须是完整的 8 字节编码")
    if data in (b"\x00" * 8, b"\xff" * 8):
        raise ValueError("ROM 不能为全 00 或全 FF")
    if crc8(data[:7]) != data[7]:
        raise ValueError("ROM 的 CRC8 校验不通过")
    return data


def rom_hex(value: object) -> str:
    return rom_bytes(value).hex().upper()


def _spreadsheet_value(value: object) -> object:
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


def export_csv(destination: str | Path, rows: Iterable[Mapping], columns: Sequence[str] | None = None) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(columns or HISTORY_COLUMNS)
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(fields)
        for row in rows:
            writer.writerow([_spreadsheet_value(row.get(field)) for field in fields])
    return path


def export_excel(destination: str | Path, rows: Iterable[Mapping], columns: Sequence[str] | None = None) -> Path:
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise RuntimeError("Excel导出需要安装openpyxl") from exc
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(columns or HISTORY_COLUMNS)
    book = Workbook(write_only=True)
    sheet = book.create_sheet("采集记录")
    sheet.append(fields)
    for index, row in enumerate(rows, start=2):
        if index > 1_048_576:
            raise ValueError("记录超过单个Excel工作表上限，请缩小时间范围")
        sheet.append([_spreadsheet_value(row.get(field)) for field in fields])
    book.save(path)
    return path


def configured_point_keys(device):
    """Current sampling scope; verified registration never invents intermediate IDs."""
    if device.get("grain_source") == "legacy":
        grain = [("grain", point) for point in range(1, device.get("grain_count", 0) + 1)]
        if device.get("ambient_mode") == "HM1500":
            return grain + [("ambient", point) for point in range(1, min(5, device.get("ambient_count", 0)) + 1)]
        if device.get("bindings_verified"):
            return grain + sorted({("ambient", r["point"]) for r in device.get("registered_points") or []
                                   if r["kind"] == "ambient"})
        return grain
    if device.get("bindings_verified"):
        return sorted({(r["kind"], r["point"]) for r in device.get("registered_points") or []})
    if device.get("registration_required"):
        return []
    return [(kind, point) for kind, count in (("grain", device.get("grain_count", 0)), ("ambient", device.get("ambient_count", 0)))
            for point in range(1, count + 1)]

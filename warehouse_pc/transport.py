"""Qt-event-driven serial requests: one in flight per physical port.

No ``waitForReadyRead``/thread sleeps are used. Read retries are bounded; writes
are never automatically repeated. A quiet/drain interval after a final timeout
reduces late-response misassociation in protocols without transaction IDs.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo

from .protocol import (
    AMBIENT_CAPACITY, BINDING_PAGE_LIMIT, GRAIN_CAPACITY,
    DeviceException, ProtocolError, crc16, lc_response_length,
    parse_response, response_length,
)
from .simulator import ProtocolSimulator

DIAGNOSTIC_EVENT_LIMIT = 1000
DIAGNOSTIC_BYTE_LIMIT = 256


def is_simulated_port(port_name: str) -> bool:
    name = port_name.strip().lower()
    return name in ("sim", "simulator") or name.startswith(("sim://", "simulator://"))


@dataclass
class _Request:
    wire: bytes
    expected: dict[str, Any]
    context: object
    timeout_ms: int
    retries: int
    attempts: int = 0
    received_bytes: int = 0
    late_bytes: int = 0
    crc_errors: int = 0
    mismatch_errors: int = 0
    discarded_partial_bytes: int = 0
    last_parse_error: str | None = None


@dataclass
class _Channel:
    profile_id: str
    port_name: str
    baud: int
    parity: str = "N"
    stop_bits: int = 1
    serial: QSerialPort | None = None
    connected: bool = False
    closing: bool = False
    queue: deque[_Request] = field(default_factory=deque)
    active: _Request | None = None
    buffer: bytearray = field(default_factory=bytearray)
    timeout: QTimer | None = None
    quiet: QTimer | None = None
    drain_limit: QTimer | None = None
    draining: bool = False
    retry_after_drain: bool = False
    quiet_ms: int = 0
    simulator: ProtocolSimulator | None = None
    epoch: int = 0


class SerialManager(QObject):
    response = Signal(str, object, bytes)
    failure = Signal(str, str)
    request_failed = Signal(str, object, str)
    connection_changed = Signal(str, bool, str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._channels: dict[str, _Channel] = {}
        self._diagnostic_events: deque[dict[str, Any]] = deque(maxlen=DIAGNOSTIC_EVENT_LIMIT)

    @staticmethod
    def _diagnostic_settings(channel: _Channel) -> dict[str, Any]:
        serial = channel.serial
        settings = {
            "profile_id": channel.profile_id, "port_name": channel.port_name,
            "baud": channel.baud, "data_bits": 8, "parity": channel.parity,
            "stop_bits": channel.stop_bits, "flow_control": "NoFlowControl",
            "connected": channel.connected,
            "source": "simulator" if is_simulated_port(channel.port_name) else "serial",
            "settings_source": "configured",
        }
        if serial is not None and serial.isOpen():
            parity = {QSerialPort.Parity.NoParity: "N", QSerialPort.Parity.EvenParity: "E",
                      QSerialPort.Parity.OddParity: "O"}
            actual_parity = serial.parity()
            settings.update(port_name=serial.portName(), baud=serial.baudRate(),
                            data_bits=serial.dataBits().value, parity=parity.get(actual_parity, actual_parity.name),
                            stop_bits=serial.stopBits().value, flow_control=serial.flowControl().name,
                            settings_source="open_qt_port")
        return settings

    def diagnostics_snapshot(self) -> dict[str, Any]:
        return {
            "time_basis": "UTC host event time. Receive chunks are host callbacks, not wire timing.",
            "channels": [self._diagnostic_settings(channel) for channel in self._channels.values()],
            "events": deepcopy(list(self._diagnostic_events)),
        }

    def _record(self, event: str, channel: _Channel, wire: bytes | bytearray = b"", **details: Any) -> None:
        item = channel.active
        entry = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event, **self._diagnostic_settings(channel),
            "byte_count": len(wire), "bytes_hex": wire[:DIAGNOSTIC_BYTE_LIMIT].hex(" ").upper(),
            "truncated": len(wire) > DIAGNOSTIC_BYTE_LIMIT,
        }
        if item is not None:
            entry.update(attempt=item.attempts, request_epoch=channel.epoch,
                         protocol=item.expected.get("protocol"), address=item.expected.get("address"),
                         command=item.expected.get("op", item.expected.get("command")))
        entry.update(details)
        self._diagnostic_events.append(entry)

    @staticmethod
    def _response_summary(item: _Request | None, buffered: int) -> dict[str, Any]:
        if item is None:
            return {}
        return {
            "received_bytes": item.received_bytes, "late_bytes": item.late_bytes,
            "crc_errors": item.crc_errors,
            "mismatch_errors": item.mismatch_errors,
            "partial_bytes": item.discarded_partial_bytes + buffered,
            "last_parse_error": item.last_parse_error,
        }

    @staticmethod
    def available_ports() -> list[dict[str, str]]:
        return [{"port_name": port.portName(), "description": port.description(),
                 "serial_number": port.serialNumber(), "system_location": port.systemLocation()}
                for port in QSerialPortInfo.availablePorts()]

    def configure(self, profile_id: str, port_name: str, baud: int = 9600,
                  parity: str = "N", stop_bits: int = 1) -> None:
        if not profile_id or not port_name.strip():
            raise ValueError("设备配置和串口名称不能为空")
        if not isinstance(baud, int) or not 300 <= baud <= 2000000:
            raise ValueError("串口波特率无效")
        parity = parity.upper()
        if parity not in ("N", "E", "O") or stop_bits not in (1, 2):
            raise ValueError("校验位或停止位无效")
        old = self._channels.get(profile_id)
        simulator = old.simulator if (old and is_simulated_port(port_name) and
            old.port_name.casefold() == port_name.strip().casefold()) else None
        if old:
            self.disconnect_port(profile_id)
            if old.timeout:
                old.timeout.deleteLater()
            if old.quiet:
                old.quiet.deleteLater()
            if old.drain_limit:
                old.drain_limit.deleteLater()
        channel = _Channel(profile_id, port_name.strip(), baud, parity, stop_bits, simulator=simulator)
        channel.timeout = QTimer(self)
        channel.timeout.setSingleShot(True)
        channel.timeout.timeout.connect(lambda: self._on_timeout(channel))
        channel.quiet = QTimer(self)
        channel.quiet.setSingleShot(True)
        channel.quiet.timeout.connect(lambda: self._end_drain(channel))
        channel.drain_limit = QTimer(self)
        channel.drain_limit.setSingleShot(True)
        channel.drain_limit.timeout.connect(lambda: self._drain_expired(channel))
        self._channels[profile_id] = channel

    def is_connected(self, profile_id: str) -> bool:
        channel = self._channels.get(profile_id)
        return bool(channel and channel.connected)

    def is_simulator(self, profile_id: str) -> bool:
        channel = self._channels.get(profile_id)
        return bool(channel and is_simulated_port(channel.port_name))

    def busy(self, profile_id: str) -> bool:
        channel = self._channels.get(profile_id)
        return bool(channel and (channel.active or channel.queue or channel.draining))

    def pending_count(self, profile_id: str) -> int:
        channel = self._channels.get(profile_id)
        return (len(channel.queue) + bool(channel.active)) if channel else 0

    def cancel_pending(self, profile_id: str) -> None:
        """Cancel queued work only; an already sent write cannot be recalled."""
        channel = self._channels.get(profile_id)
        if channel:
            cancelled = list(channel.queue)
            channel.queue.clear()
            for item in cancelled:
                self.request_failed.emit(profile_id, item.context, "排队请求已取消")

    def connect_port(self, profile_id: str) -> None:
        channel = self._channels.get(profile_id)
        if channel is None:
            self.failure.emit(profile_id, "请先配置串口")
            return
        if channel.connected:
            return
        channel.closing = False
        if is_simulated_port(channel.port_name):
            if channel.simulator is None:
                channel.simulator = ProtocolSimulator()
            channel.connected = True
            self.connection_changed.emit(profile_id, True, "模拟器已连接（非真实设备数据）")
            return
        for other in self._channels.values():
            if other is not channel and other.connected and other.port_name.casefold() == channel.port_name.casefold():
                self.failure.emit(profile_id, "此串口已被另一设备配置占用，请共用同一串口队列")
                return
        serial = QSerialPort(self)
        channel.serial = serial
        serial.setPortName(channel.port_name)
        serial.setBaudRate(channel.baud)
        serial.setDataBits(QSerialPort.DataBits.Data8)
        serial.setParity({"N": QSerialPort.Parity.NoParity, "E": QSerialPort.Parity.EvenParity,
                          "O": QSerialPort.Parity.OddParity}[channel.parity])
        serial.setStopBits(QSerialPort.StopBits.OneStop if channel.stop_bits == 1 else QSerialPort.StopBits.TwoStop)
        serial.setFlowControl(QSerialPort.FlowControl.NoFlowControl)
        serial.readyRead.connect(lambda: self._read_ready(channel))
        serial.errorOccurred.connect(lambda error: self._serial_error(channel, error))
        if not serial.open(QSerialPort.OpenModeFlag.ReadWrite):
            message = serial.errorString()
            self._record("FAIL", channel, reason="open_error", message=message)
            channel.serial = None
            serial.deleteLater()
            self.failure.emit(profile_id, f"无法打开 {channel.port_name}：{message}")
            self.connection_changed.emit(profile_id, False, message)
            return
        channel.connected = True
        serial.clear()
        if not channel.connected:
            return
        self.connection_changed.emit(profile_id, True,
            f"{channel.port_name} 已连接 · {channel.baud} 8{channel.parity}{channel.stop_bits}")

    def disconnect_port(self, profile_id: str) -> None:
        channel = self._channels.get(profile_id)
        if channel is None:
            return
        was_connected = channel.connected
        channel.closing = True
        channel.connected = False
        channel.epoch += 1
        if channel.timeout:
            channel.timeout.stop()
        if channel.quiet:
            channel.quiet.stop()
        if channel.drain_limit:
            channel.drain_limit.stop()
        cancelled = ([channel.active] if channel.active else []) + list(channel.queue)
        channel.active = None
        channel.queue.clear()
        channel.buffer.clear()
        channel.draining = channel.retry_after_drain = False
        if channel.serial:
            channel.serial.close()
            channel.serial.deleteLater()
            channel.serial = None
        if was_connected:
            self.connection_changed.emit(profile_id, False, "已断开")
        for item in cancelled:
            self.request_failed.emit(profile_id, item.context, "串口已断开，请求取消")

    def close_all(self) -> None:
        for profile_id in tuple(self._channels):
            self.disconnect_port(profile_id)

    @staticmethod
    def _expected(request: bytes, expected: dict[str, Any]) -> dict[str, Any]:
        context = dict(expected)
        protocol = context.get("protocol")
        if protocol == "lc":
            if len(request) != 22 or request[0] != 0xF7:
                raise ProtocolError("LC请求必须为22字节")
            address, command = request[2], request[3]
            if "address" in context and context["address"] != address:
                raise ProtocolError("请求地址与预期设备不一致")
            if "command" in context and context["command"] != command:
                raise ProtocolError("请求命令与预期不一致")
            context.update(address=address, command=command)
            if command in (0xD0, 0xD1):
                for key, value in (("start", request[4]), ("count", request[5]), ("ambient_channel", request[6])):
                    if key in context and context[key] != value:
                        raise ProtocolError("LC请求参数与预期不一致")
                    context[key] = value
            elif command == 0xD2:
                context.setdefault("point", request[4])
                context.setdefault("start", request[4])
            lc_response_length(command, **{key: value for key, value in context.items() if key != "command"})
            return context
        if protocol not in ("modbus", "maintenance") or not 4 <= len(request) <= 256:
            raise ProtocolError("请求协议或长度无效")
        if not 1 <= request[0] <= 247 or crc16(request[:-2]) != int.from_bytes(request[-2:], "little"):
            raise ProtocolError("请求地址或CRC无效")
        if "address" in context and context["address"] != request[0]:
            raise ProtocolError("请求地址与预期不一致")
        context["address"] = request[0]
        if protocol == "modbus":
            if len(request) != 8 or request[1] != 3:
                raise ProtocolError("仅支持03寄存器读请求")
            start, count = int.from_bytes(request[2:4], "big"), int.from_bytes(request[4:6], "big")
            for key, value in (("start", start), ("count", count)):
                if key in context and context[key] != value:
                    raise ProtocolError("寄存器请求与预期不一致")
                context[key] = value
            if not 1 <= count <= 125:
                raise ProtocolError("寄存器数量无效")
        else:
            if len(request) < 5 or request[1] != 0x41:
                raise ProtocolError("维护请求格式无效")
            if "op" in context and context["op"] != request[2]:
                raise ProtocolError("维护请求操作码与预期不一致")
            context["op"] = request[2]
            payload = request[3:-2]
            if request[2] in (7, 8) and len(payload) >= 3:
                kind = {1: "grain", 2: "ambient"}.get(payload[0])
                context.setdefault("kind", kind)
                context.setdefault("point", int.from_bytes(payload[1:3], "big"))
                if request[2] == 7 and len(payload) == 4:
                    context.setdefault("table", payload[3])
            if request[2] == 2 and len(payload) == 3:
                context.setdefault("offset", int.from_bytes(payload[:2], "big"))
                context.setdefault("scan_count", payload[2])
            if request[2] == 9:
                if len(payload) != 4:
                    raise ProtocolError("绑定分页请求长度无效")
                table, offset, limit = payload[0], int.from_bytes(payload[1:3], "big"), payload[3]
                if table not in (0, 1) or offset > GRAIN_CAPACITY + AMBIENT_CAPACITY or not 1 <= limit <= BINDING_PAGE_LIMIT:
                    raise ProtocolError("绑定分页请求的表、偏移或条数无效")
                for key, value in (("table", table), ("offset", offset), ("limit", limit)):
                    if key in context and context[key] != value:
                        raise ProtocolError("绑定分页请求与预期不一致")
                    context[key] = value
        return context

    def submit(self, profile_id: str, request: bytes, expected: dict[str, Any], context: object = None) -> None:
        channel = self._channels.get(profile_id)
        if channel is None or not channel.connected:
            self.request_failed.emit(profile_id, context, "串口尚未连接")
            self.failure.emit(profile_id, "串口尚未连接")
            return
        if len(channel.queue) >= 512:
            self.request_failed.emit(profile_id, context, "串口请求队列已满")
            self.failure.emit(profile_id, "串口请求队列已满，请等待当前操作完成")
            return
        try:
            request = bytes(request)
            expected = self._expected(request, expected)
            safe_read = expected["protocol"] == "modbus" or (
                expected["protocol"] == "lc" and expected["command"] in (0xD0, 0xD1, 0xD2, 0xD8, 0xD9)) or (
                expected["protocol"] == "maintenance" and expected["op"] in (0, 2, 7, 8, 9))
            retries = min(2, max(0, int(expected.get("retries", 1)))) if safe_read else 0
            bits = 1 + 8 + (channel.parity != "N") + channel.stop_bits
            max_reply = lc_response_length(expected["command"], **{key: value for key, value in expected.items() if key != "command"}) if expected["protocol"] == "lc" else 256
            default_timeout = max(3000, int((len(request) + max_reply) * bits * 1000 / channel.baud + 1500))
            if expected["protocol"] == "maintenance" and expected["op"] == 5:
                default_timeout = max(default_timeout, 6000)
            timeout = int(expected.get("timeout_ms", default_timeout))
            if not 20 <= timeout <= 120000:
                raise ProtocolError("请求超时需在20～120000毫秒之间")
        except (ValueError, TypeError, KeyError) as error:
            self._record("FAIL", channel, reason="invalid_request", message=str(error))
            self.request_failed.emit(profile_id, context, str(error))
            self.failure.emit(profile_id, str(error))
            return
        channel.queue.append(_Request(request, expected, context, timeout, retries))
        self._advance(channel)

    def _advance(self, channel: _Channel) -> None:
        if not channel.connected or channel.active or channel.draining or not channel.queue:
            return
        channel.active = channel.queue.popleft()
        self._transmit(channel)

    def _transmit(self, channel: _Channel) -> None:
        item = channel.active
        if not channel.connected or item is None:
            return
        item.attempts += 1
        channel.epoch += 1
        channel.buffer.clear()
        serial = channel.serial
        if serial is not None:
            serial.clear(QSerialPort.Direction.Input)
            if channel.active is not item:
                return
        self._record("TX_SUBMIT", channel, item.wire, timeout_ms=item.timeout_ms)
        if serial is not None:
            written = serial.write(item.wire)
            if channel.active is not item:
                return
            if written != len(item.wire):
                self._finish_failure(channel, "串口请求写入失败", reason="write_error")
                return
        assert channel.timeout is not None
        channel.timeout.start(item.timeout_ms)
        if is_simulated_port(channel.port_name):
            epoch = channel.epoch
            QTimer.singleShot(20, lambda: self._simulate(channel, item, epoch))

    def _simulate(self, channel: _Channel, item: _Request, epoch: int) -> None:
        if not channel.connected or channel.active is not item or epoch != channel.epoch:
            return
        try:
            assert channel.simulator is not None
            wire = channel.simulator.simulate_request(item.wire, item.expected)
        except Exception as error:
            self._finish_failure(channel, f"模拟器错误：{error}", reason="simulator_error")
            return
        if wire:
            # Exercise the same stream parser used for real ports, in fragments.
            split = max(1, len(wire) // 2)
            self._receive(channel, wire[:split])
            self._receive(channel, wire[split:])

    def _read_ready(self, channel: _Channel) -> None:
        if channel.serial:
            self._receive(channel, bytes(channel.serial.readAll()))

    def _receive(self, channel: _Channel, incoming: bytes) -> None:
        if not incoming or not channel.connected:
            return
        disposition = "draining" if channel.draining else "in_flight" if channel.active else "unsolicited"
        self._record("RX_CHUNK", channel, incoming, disposition=disposition)
        if channel.draining:
            if channel.active:
                channel.active.received_bytes += len(incoming)
                channel.active.late_bytes += len(incoming)
            assert channel.quiet is not None
            channel.quiet.start(channel.quiet_ms)
            return
        if channel.active is None:
            return  # Unsolicited/late traffic cannot become a future response.
        item = channel.active
        item.received_bytes += len(incoming)
        channel.buffer.extend(incoming)
        if len(channel.buffer) > 4096:
            item.discarded_partial_bytes += len(channel.buffer)
            item.last_parse_error = "接收缓冲超过上限"
            channel.buffer.clear()
            return
        while channel.buffer and channel.active is item:
            try:
                length = response_length(channel.buffer, item.expected)
            except ProtocolError as error:
                item.mismatch_errors += 1
                item.last_parse_error = str(error)
                del channel.buffer[0]
                continue
            if length is None or len(channel.buffer) < length:
                return
            wire = bytes(channel.buffer[:length])
            try:
                parsed = parse_response(wire, item.expected)
            except DeviceException as error:
                self._finish_failure(channel, str(error), reason="device_exception")
                return
            except ProtocolError as error:
                item.last_parse_error = str(error)
                if "CRC" in item.last_parse_error.upper():
                    item.crc_errors += 1
                else:
                    item.mismatch_errors += 1
                del channel.buffer[0]
                continue
            channel.timeout.stop()
            self._record("PARSE_OK", channel, wire, result=parsed.get("result"),
                         **self._response_summary(item, 0))
            channel.active = None
            channel.buffer.clear()
            simulated = is_simulated_port(channel.port_name)
            parsed.update(simulated=simulated, source="simulator" if simulated else "serial", port_name=channel.port_name)
            # Give Qt a turn and preserve frame silence before the next request.
            bits = 1 + 8 + (channel.parity != "N") + channel.stop_bits
            gap_ms = max(5, int(3.5 * bits * 1000 / channel.baud) + 1)
            self._begin_drain(channel, gap_ms)
            self.response.emit(channel.profile_id, {"context": item.context, "parsed": parsed}, wire)
            return

    def _begin_drain(self, channel: _Channel, milliseconds: int, *, retry: bool = False) -> None:
        if not channel.connected:
            return
        channel.buffer.clear()
        channel.draining = True
        channel.retry_after_drain = retry
        channel.quiet_ms = milliseconds
        assert channel.quiet is not None
        channel.quiet.start(milliseconds)
        assert channel.drain_limit is not None
        channel.drain_limit.start(max(1000, milliseconds * 3))

    def _end_drain(self, channel: _Channel) -> None:
        if channel.drain_limit:
            channel.drain_limit.stop()
        channel.draining = False
        if channel.retry_after_drain:
            channel.retry_after_drain = False
            self._transmit(channel)
        else:
            self._advance(channel)

    def _drain_expired(self, channel: _Channel) -> None:
        # Continuous unsolicited bytes must not keep a retry/queue alive forever.
        if not channel.connected or not channel.draining:
            return
        self._record("FAIL", channel, reason="continuous_unexpected_data",
                     **self._response_summary(channel.active, len(channel.buffer)))
        self.disconnect_port(channel.profile_id)
        self.failure.emit(channel.profile_id, "串口持续收到非预期数据，无法恢复静默，已断开连接")

    def _on_timeout(self, channel: _Channel) -> None:
        item = channel.active
        if item is None or not channel.connected:
            return
        if item.attempts <= item.retries:
            self._record("FAIL", channel, channel.buffer, reason="timeout_retry",
                         **self._response_summary(item, len(channel.buffer)))
            item.discarded_partial_bytes += len(channel.buffer)
            self._begin_drain(channel, 100, retry=True)
            return
        self._finish_failure(channel, "请求超时，未获得匹配的完整响应", quarantine=True,
                             reason="no_data" if not item.received_bytes else "unmatched_data")

    def _finish_failure(self, channel: _Channel, message: str, *, quarantine: bool = False,
                        reason: str = "request_error") -> None:
        if channel.timeout:
            channel.timeout.stop()
        item = channel.active
        self._record("FAIL", channel, channel.buffer, reason=reason, message=message,
                     **self._response_summary(item, len(channel.buffer)))
        channel.active = None
        channel.buffer.clear()
        # Set draining before emitting signals: callbacks may submit new work.
        delay = min(5000, max(500, item.timeout_ms)) if quarantine and item else 10
        self._begin_drain(channel, delay)
        if item:
            self.request_failed.emit(channel.profile_id, item.context, message)
        self.failure.emit(channel.profile_id, message)

    def _serial_error(self, channel: _Channel, error: QSerialPort.SerialPortError) -> None:
        if channel.closing or not channel.connected or error == QSerialPort.SerialPortError.NoError:
            return
        fatal = (QSerialPort.SerialPortError.ResourceError, QSerialPort.SerialPortError.DeviceNotFoundError,
                 QSerialPort.SerialPortError.ReadError, QSerialPort.SerialPortError.WriteError,
                 QSerialPort.SerialPortError.PermissionError)
        message = channel.serial.errorString() if channel.serial else "串口错误"
        self._record("FAIL", channel, channel.buffer, reason="serial_error", message=message,
                     error=error.name, **self._response_summary(channel.active, len(channel.buffer)))
        if error in fatal:
            self.disconnect_port(channel.profile_id)
            self.failure.emit(channel.profile_id, message)

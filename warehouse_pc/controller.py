"""Application orchestration independent of the individual desktop pages."""
from __future__ import annotations

import dataclasses
import time
import uuid
from datetime import datetime

from PySide6.QtCore import QObject, QTimer, Signal

from .protocol import build_lc_request, build_modbus_read
from .transport import SerialManager


class Controller(QObject):
    metadata_changed = Signal(object)
    samples_changed = Signal(int, object)
    connections_changed = Signal()
    alarms_changed = Signal(object)
    log = Signal(str)
    running_changed = Signal(bool)

    def __init__(self, repository, demo=False, parent=None):
        super().__init__(parent)
        self.repo, self.demo = repository, demo
        self.manager = SerialManager(self)
        self.metadata = {"warehouses": [], "ports": [], "devices": []}
        self.latest = {}
        self.last_received = {}
        self.last_stored = {}
        self.connected = {}
        self.paused = set()
        self.cycles = {}
        self.processing_devices = set()
        self.running = False
        self.history_interval = 30
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.poll)
        self.alarm_timer = QTimer(self)
        self.alarm_timer.setInterval(5000)
        self.alarm_timer.timeout.connect(self.refresh_alarms)
        self.manager.response.connect(self._response)
        self.manager.failure.connect(lambda profile, error: self.log.emit(f"连接 {profile}：{error}"))
        self.manager.request_failed.connect(self._failed)
        self.manager.connection_changed.connect(self._connection)
        self.repo.ready.connect(self.initialize)

    def initialize(self):
        def after_settings(value):
            value = value if isinstance(value, dict) else {}
            self.timer.setInterval(max(1, int(value.get("poll_seconds", 5))) * 1000)
            self.history_interval = max(1, int(value.get("history_seconds", 30)))
            if self.demo:
                self.repo.call("demo_seed", callback=lambda _: self.reload_metadata(auto_start=True))
            else:
                self.reload_metadata()
            self.alarm_timer.start()
        self.repo.call("get_setting", "collection", {}, callback=after_settings)

    def reload_metadata(self, auto_start=False, callback=None, on_error=None):
        def loaded(metadata):
            old_devices = {d["id"]: d for d in self.metadata["devices"]}
            new_devices = {d["id"]: d for d in metadata["devices"]}
            for identifier, old in old_devices.items():
                if identifier not in new_devices or self._endpoint(old) != self._endpoint(new_devices[identifier]):
                    self.latest.pop(identifier, None)
                    self.last_received.pop(identifier, None)
                    self.last_stored.pop(identifier, None)
                    self.cycles = {token: cycle for token, cycle in self.cycles.items()
                                   if cycle["device"]["id"] != identifier}
            self.metadata = metadata
            self.metadata_changed.emit(metadata)
            self.refresh_alarms()
            if auto_start:
                self.connect_all()
                self.set_running(True)
            if callback:
                callback(metadata)
        self.repo.call("metadata", callback=loaded, on_error=on_error)

    @staticmethod
    def _endpoint(device):
        return tuple(device.get(key) for key in (
            "port_id", "port_name", "baud", "parity", "stop_bits", "address", "protocol", "source", "grain_count", "ambient_count"))

    def device(self, identifier):
        return next((d for d in self.metadata["devices"] if d["id"] == identifier), None)

    def connect_port(self, port):
        profile = str(port["id"])
        if port["source"] != ("simulator" if self.demo else "serial"):
            self.repo.error.emit("连接来源与当前数据模式不同，请使用对应的模拟或实机入口。")
            return
        if not self.manager.is_connected(profile):
            self.manager.configure(profile, port["port_name"], int(port["baud"]),
                                   parity=port.get("parity", "N"), stop_bits=int(port.get("stop_bits", 1)))
            self.manager.connect_port(profile)

    def connect_all(self):
        for port in self.metadata["ports"]:
            self.connect_port(port)

    def disconnect_all(self):
        self.set_running(False)
        self.manager.close_all()
        self.cycles.clear()

    def _connection(self, profile, connected, message):
        self.connected[profile] = connected
        self.connections_changed.emit()
        self.log.emit(message)

    def set_running(self, running):
        self.running = running
        if running:
            self.timer.start()
            self.poll()
        else:
            self.timer.stop()
        self.running_changed.emit(running)

    def set_intervals(self, poll_seconds, history_seconds):
        self.timer.setInterval(int(poll_seconds) * 1000)
        self.history_interval = int(history_seconds)
        self.repo.call("set_setting", "collection", {
            "poll_seconds": int(poll_seconds), "history_seconds": int(history_seconds)})

    def _requests(self, device):
        address = device["address"]
        grain, ambient = device["grain_count"], device["ambient_count"]
        jobs = []
        if device["protocol"] == "LC-M3":
            for start in range(1, grain + 1, 32):
                count = min(32, grain + 1 - start)
                channel = 1 if ambient else 0
                expected = {"protocol": "lc", "address": address, "command": 0xD0,
                            "start": start, "count": count, "ambient_channel": channel}
                jobs.append((build_lc_request(address, 0xD0, bytes((start, count, channel))), expected))
            for point in range(2 if grain else 1, ambient + 1):
                jobs.append((build_lc_request(address, 0xD2, bytes((point,))),
                             {"protocol": "lc", "address": address, "command": 0xD2, "point": point}))
        else:
            for start in range(1, grain + 1, 125):
                count = min(125, grain + 1 - start)
                jobs.append((build_modbus_read(address, start, count),
                             {"protocol": "modbus", "address": address, "start": start, "count": count}))
                status = 0x4000 + start - 1
                jobs.append((build_modbus_read(address, status, count),
                             {"protocol": "modbus", "address": address, "start": status, "count": count}))
            if ambient:
                jobs.append((build_modbus_read(address, 0x2000, ambient * 2),
                             {"protocol": "modbus", "address": address, "start": 0x2000, "count": ambient * 2}))
                jobs.append((build_modbus_read(address, 0x40A5, ambient),
                             {"protocol": "modbus", "address": address, "start": 0x40A5, "count": ambient}))
        return jobs

    def poll(self):
        eligible = {str(p["id"]) for p in self.metadata["ports"]
                    if str(p["id"]) not in self.paused and self.manager.is_connected(str(p["id"]))
                    and not self.manager.busy(str(p["id"]))}
        for device in self.metadata["devices"]:
            profile = str(device["port_id"])
            if profile not in eligible:
                continue
            if device["id"] in self.processing_devices or any(c["device"]["id"] == device["id"] for c in self.cycles.values()):
                continue
            jobs = self._requests(device)
            token = uuid.uuid4().hex
            self.cycles[token] = {"device": device.copy(), "remaining": len(jobs), "samples": {}, "statuses": {}}
            for request, expected in jobs:
                self.manager.submit(profile, request, expected, {"owner": "poll", "cycle": token})

    def query_online(self, device):
        address = device["address"]
        if device["protocol"] == "LC-M3":
            request = build_lc_request(address, 0xD9)
            expected = {"protocol": "lc", "address": address, "command": 0xD9}
        else:
            request = build_modbus_read(address, 0x100, 1)
            expected = {"protocol": "modbus", "address": address, "start": 0x100, "count": 1}
        self.manager.submit(str(device["port_id"]), request, expected,
                            {"owner": "ping", "device_id": device["id"]})

    def _response(self, profile, packet, wire):
        context, parsed = packet.get("context"), packet.get("parsed", {})
        if not isinstance(context, dict):
            return
        if context.get("owner") == "ping":
            device = self.device(context["device_id"])
            self.log.emit(f"{device['name'] if device else '分机'}：在线查询成功")
            return
        if context.get("owner") != "poll":
            return
        cycle = self.cycles.get(context.get("cycle"))
        if cycle is None:
            return
        for sample in parsed.get("samples", []):
            row = dataclasses.asdict(sample) if dataclasses.is_dataclass(sample) else sample.copy()
            cycle["samples"][(row["kind"], row["point"])] = row
        for row in parsed.get("statuses", []):
            cycle["statuses"][(row["kind"], row["point"])] = row["status"]
        cycle["remaining"] -= 1
        if not cycle["remaining"]:
            self._finish_cycle(context["cycle"])

    def _failed(self, profile, context, error):
        if not isinstance(context, dict) or context.get("owner") != "poll":
            return
        cycle = self.cycles.get(context.get("cycle"))
        if cycle:
            cycle["remaining"] -= 1
            if not cycle["remaining"]:
                self._finish_cycle(context["cycle"])

    def _finish_cycle(self, token):
        cycle = self.cycles.pop(token)
        device = cycle["device"]
        rows = []
        for kind, capacity in (("grain", device["grain_count"]), ("ambient", device["ambient_count"])):
            for point in range(1, capacity + 1):
                row = cycle["samples"].get((kind, point), {
                    "kind": kind, "point": point, "temperature": None, "humidity": None, "status": 3})
                if (kind, point) in cycle["statuses"]:
                    row["status"] = cycle["statuses"][(kind, point)]
                    # A status reply alone must not turn a missing value into a normal sample.
                    if row["status"] == 0 and row.get("temperature") is None:
                        row["status"] = 255
                rows.append(row)
        identifier = device["id"]
        self.processing_devices.add(identifier)
        stamp = datetime.now().isoformat(timespec="seconds")
        now = time.monotonic()
        persist = now - self.last_stored.get(identifier, -1e10) >= self.history_interval
        method = "save_samples" if persist else "preview_samples"

        def processed(result):
            self.processing_devices.discard(identifier)
            current = self.device(identifier)
            if current is None or self._endpoint(current) != self._endpoint(device):
                return
            self.latest[identifier] = result
            self.last_received[identifier] = now
            if persist:
                self.last_stored[identifier] = now
            self.samples_changed.emit(identifier, result)
            alarm_rows = result
            if time.monotonic() - now > max(30, 3 * self.timer.interval() / 1000):
                alarm_rows = [dict(row, status="stale") for row in result]
            self.repo.call("evaluate_alarms", identifier, alarm_rows,
                           callback=lambda _: self.refresh_alarms())
        def failed(message):
            self.processing_devices.discard(identifier)
            self.repo.error.emit(message)
        self.repo.call(method, identifier, rows, timestamp=stamp, source=device["source"],
                       callback=processed, on_error=failed)

    def refresh_alarms(self):
        self.repo.call("alarms", limit=500, callback=self.alarms_changed.emit)

    def pause_port(self, profile):
        self.paused.add(profile)

    def resume_port(self, profile):
        self.paused.discard(profile)

    def shutdown(self):
        self.timer.stop()
        self.alarm_timer.stop()
        self.manager.close_all()

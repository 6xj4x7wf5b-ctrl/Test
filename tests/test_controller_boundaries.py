"""Small regressions for endpoint changes, delayed storage, and registration.

No worker thread, serial hardware, or filesystem data is required. Repository
callbacks are completed deliberately out of order to exercise GUI boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox

import warehouse_pc.controller as controller_module
import warehouse_pc.devices_ui as devices_module


@dataclass
class Job:
    method: str
    args: tuple
    kwargs: dict
    finished: bool = False

    def complete(self, result):
        assert not self.finished
        self.finished = True
        callback = self.kwargs.get("callback")
        if callback:
            callback(result)


class DeferredRepository(QObject):
    ready = Signal()
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self.jobs: list[Job] = []

    def call(self, method, *args, **kwargs):
        self.jobs.append(Job(method, args, kwargs))
        return len(self.jobs)

    def pending(self, method):
        return next(job for job in self.jobs if job.method == method and not job.finished)


def device(identifier=1, **changes):
    result = dict(id=identifier, name=f"分机 {identifier}", warehouse_id=1,
                  port_id=identifier, address=1, protocol="LC-M3", source="serial",
                  grain_count=2, ambient_count=0)
    result.update(changes)
    return result


def metadata(*devices):
    return {"devices": list(devices), "ports": [], "warehouses": []}


def completed_cycle(controller, configured_device, token="capture"):
    controller.cycles[token] = {
        "device": configured_device.copy(), "remaining": 0, "statuses": {},
        "samples": {("grain", 1): {"kind": "grain", "point": 1,
                                    "temperature": 22.0, "humidity": None, "status": 0}},
    }
    controller._finish_cycle(token)


@pytest.fixture
def control(qt_application):
    repository = DeferredRepository()
    controller = controller_module.Controller(repository)
    controller.metadata = metadata(device())
    yield controller, repository
    controller.shutdown()
    controller.deleteLater()
    repository.deleteLater()


@pytest.mark.parametrize("changes", [
    {"address": 2}, {"port_id": 7}, {"protocol": "Modbus RTU"},
    {"grain_count": 3}, {"ambient_count": 1},
])
def test_changed_endpoint_drops_previous_values_and_inflight_cycle(control, changes):
    controller, repository = control
    original, untouched = device(), device(2)
    controller.metadata = metadata(original, untouched)
    controller.latest = {1: [{"temp": 22.0}], 2: [{"temp": 18.0}]}
    controller.last_received = {1: 100.0, 2: 101.0}
    controller.last_stored = {1: 100.0, 2: 101.0}
    controller.cycles = {
        "changed": {"device": original.copy()},
        "untouched": {"device": untouched.copy()},
    }
    controller.reload_metadata()
    repository.pending("metadata").complete(metadata(device(**changes), untouched))

    assert 1 not in controller.latest
    assert 1 not in controller.last_received
    assert 1 not in controller.last_stored
    assert "changed" not in controller.cycles
    assert controller.latest[2] == [{"temp": 18.0}]
    assert controller.last_received[2] == 101.0
    assert "untouched" in controller.cycles
    controller.cycles.clear()


def test_name_only_edit_keeps_same_endpoint_recent_values(control):
    controller, repository = control
    controller.latest[1] = [{"temp": 22.0}]
    controller.last_received[1] = 100.0
    controller.reload_metadata()
    repository.pending("metadata").complete(metadata(device(name="新分机名称")))
    assert controller.latest[1] == [{"temp": 22.0}]
    assert controller.last_received[1] == 100.0


@pytest.mark.parametrize("removed", [False, True])
def test_old_database_callback_cannot_restore_values_for_changed_or_removed_device(control, removed):
    controller, repository = control
    completed_cycle(controller, device())
    stored = repository.pending("save_samples")
    emitted = []
    controller.samples_changed.connect(lambda *args: emitted.append(args))

    controller.reload_metadata()
    repository.pending("metadata").complete(metadata() if removed else metadata(device(address=2)))
    stored.complete([{"device_id": 1, "kind": "grain", "point": 1,
                      "temp": 22.0, "status": "ok"}])

    assert not emitted
    assert 1 not in controller.latest
    assert 1 not in controller.last_received
    assert 1 not in controller.processing_devices
    assert not any(job.method == "evaluate_alarms" for job in repository.jobs)


@pytest.mark.parametrize("delay, alarm_status", [(5.0, "ok"), (100.0, "stale")])
def test_storage_delay_keeps_capture_time_and_does_not_trigger_fresh_alarm(control, monkeypatch,
                                                                         delay, alarm_status):
    controller, repository = control
    clock = [100.0]
    monkeypatch.setattr(controller_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    completed_cycle(controller, device())
    assert 1 in controller.processing_devices
    stored = repository.pending("save_samples")
    clock[0] += delay
    result = [{"device_id": 1, "kind": "grain", "point": 1,
               "raw_temp": 22.0, "temp": 22.0, "humidity": None, "status": "ok"}]
    stored.complete(result)

    assert controller.last_received[1] == 100.0
    assert controller.last_stored[1] == 100.0
    assert 1 not in controller.processing_devices
    alarm_job = repository.pending("evaluate_alarms")
    assert alarm_job.args[1][0]["status"] == alarm_status
    assert result[0]["status"] == "ok"  # Stored history must not be rewritten.
    assert controller.latest[1][0]["temp"] == 22.0


class FakeManager:
    def __init__(self):
        self.connected = True
        self.is_busy = True

    def is_connected(self, profile):
        return self.connected

    def busy(self, profile):
        return self.is_busy

    def close_all(self):
        self.connected = False


@pytest.fixture
def registration_page(control, monkeypatch):
    controller, repository = control
    controller.manager = FakeManager()
    page = devices_module.DevicesPage(repository, controller)
    monkeypatch.setattr(page, "selected", lambda category: device())
    opened, notices = [], []

    from PySide6.QtWidgets import QDialog
    from PySide6.QtCore import Signal

    class Dialog(QDialog):
        bindings_saved = Signal(object)
        bindings_loaded = Signal(object)
        def __init__(self, *args, **kwargs):
            super().__init__(page)

        def show(self):
            opened.append(1)
            page.register()
            super().show()

    monkeypatch.setattr(devices_module, "RegistrationDialog", Dialog)
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: notices.append(args[-1]))
    yield page, controller, opened, notices
    if page._registration_wait:
        page._registration_wait.stop()
    page.close()
    page.deleteLater()


def test_repeated_registration_activation_has_one_wait_timer_and_one_dialog(registration_page):
    page, controller, opened, notices = registration_page
    page.register()
    timer = page._registration_wait
    assert timer is not None and timer.isActive()
    assert "1" in controller.paused
    page.register()
    assert page._registration_wait is timer
    assert not opened

    controller.manager.is_busy = False
    timer.timeout.emit()
    assert opened == [1]
    assert not timer.isActive()
    assert page._registration_wait is None
    assert page._registration_active
    assert "1" in controller.paused
    page.registration_dialog.reject()
    assert not page._registration_active
    assert "1" not in controller.paused
    assert not notices


def test_disconnect_while_waiting_cancels_registration_and_releases_pause(registration_page):
    page, controller, opened, notices = registration_page
    page.register()
    timer = page._registration_wait
    controller.manager.connected = False
    timer.timeout.emit()
    assert not opened
    assert not timer.isActive()
    assert page._registration_wait is None
    assert not page._registration_active
    assert "1" not in controller.paused


def test_registration_busy_timeout_stops_its_timer_and_releases_pause(registration_page, monkeypatch):
    page, controller, opened, notices = registration_page
    clock = [100.0]
    monkeypatch.setattr(devices_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    page.register()
    timer = page._registration_wait
    clock[0] += 61.0
    timer.timeout.emit()
    assert not opened
    assert not timer.isActive()
    assert not page._registration_active
    assert page._registration_wait is None
    assert "1" not in controller.paused
    assert not notices
    assert "串口仍忙" in page.registration_hint.text()

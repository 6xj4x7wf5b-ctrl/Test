"""End-to-end desktop/controller/database tests, with no physical devices."""
import time

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from warehouse_pc.app import MainWindow


def until(app, predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(5)
    assert predicate(), "异步应用操作未在预期时间完成"


def call(app, repo, method, *args, **kwargs):
    result, errors = [], []
    repo.call(method, *args, callback=result.append, on_error=errors.append, **kwargs)
    until(app, lambda: bool(result or errors))
    assert not errors, errors
    return result[0]


def test_empty_application_has_no_login_or_seed_data(qt_application, tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: warnings.append(a[-1]))
    window = MainWindow(tmp_path, demo=False)
    window.show()
    try:
        until(qt_application, lambda: window.repo.thread.isRunning())
        assert call(qt_application, window.repo, "devices") == []
        assert call(qt_application, window.repo, "history_count") == 0
        for i in range(window.stack.count()):
            window.navigate(i)
            qt_application.processEvents()
        assert not warnings
    finally:
        window.close()
        until(qt_application, lambda: not window.repo.thread.isRunning())
        qt_application.processEvents()


def test_two_serial_profiles_end_to_end(qt_application, tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: warnings.append(a[-1]))
    window = MainWindow(tmp_path / "demo", demo=True)
    window.show()
    controller = window.controller
    try:
        until(qt_application, lambda: len(controller.latest) == 2)
        controller.set_running(False)
        devices = controller.metadata["devices"]
        assert {d["protocol"] for d in devices} == {"LC-M3", "Modbus RTU"}
        assert len(controller.metadata["ports"]) == 2
        assert all(d["address"] == 1 for d in devices)
        full = next(d for d in devices if d["grain_count"] == 165)
        assert any(r["kind"] == "grain" and r["point"] == 165 for r in controller.latest[full["id"]])
        assert all(r["source"] == "simulator" for rows in controller.latest.values() for r in rows)
        assert any(r["raw_temp"] == -19.0 for r in controller.latest[full["id"]])
        identifier = full["id"]
        call(qt_application, window.repo, "save_calibration", identifier, "grain", 1, 2.5, 0)
        initial = call(qt_application, window.repo, "history_count", device_id=identifier)
        last = controller.last_received[identifier]
        controller.poll()
        until(qt_application, lambda: controller.last_received[identifier] > last)
        row = next(r for r in controller.latest[identifier] if r["kind"] == "grain" and r["point"] == 1)
        assert row["temp"] == row["raw_temp"] + 2.5
        # Polling updates the screen without duplicating history inside its interval.
        assert call(qt_application, window.repo, "history_count", device_id=identifier) == initial
        call(qt_application, window.repo, "save_threshold", identifier, "grain", temp_high=-100)
        last = controller.last_received[identifier]
        controller.poll()
        until(qt_application, lambda: controller.last_received[identifier] > last)
        until(qt_application, lambda: any(r["device_id"] == identifier and r["active"] for r in window.alarm_rows))
        profile_other = str(next(d for d in devices if d["id"] != identifier)["port_id"])
        controller.manager.disconnect_port(profile_other)
        last = controller.last_received[identifier]
        controller.poll()
        until(qt_application, lambda: controller.last_received[identifier] > last)
        assert controller.manager.is_connected(str(full["port_id"]))
        for i in range(window.stack.count()):
            window.navigate(i)
            qt_application.processEvents()
        assert not warnings, warnings
    finally:
        window.close()
        until(qt_application, lambda: not window.repo.thread.isRunning())
        qt_application.processEvents()

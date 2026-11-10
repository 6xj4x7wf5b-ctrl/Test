"""Exercise device navigation, embedded registration and sparse polling together."""

import time

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from warehouse_pc.app import MainWindow


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(5)
    assert predicate(), "应用操作未在预期时间完成"


def point_ids(rows):
    return {(row["kind"], row["point"]) for row in rows}


def test_registration_navigation_persists_sparse_points_and_resumes_port(qt_application, tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[-1]))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window = MainWindow(tmp_path / "demo", demo=True)
    window.show()
    controller, page = window.controller, window.devices
    manager = controller.manager

    def navigate(label):
        next(button for button, _, name, _ in window.nav_routes if name == label).click()

    try:
        wait_for(lambda: len(controller.latest) == 2)
        controller.timer.stop()  # Drive the same poll entry point deterministically.
        devices = controller.metadata["devices"]
        target = next(device for device in devices if device["protocol"] == "Modbus RTU")
        other = next(device for device in devices if device["id"] != target["id"])
        target_id, other_id = target["id"], other["id"]
        profile, other_profile = str(target["port_id"]), str(other["port_id"])
        wait_for(lambda: not manager.busy(profile) and not manager.busy(other_profile))

        views = []
        for label in ("测温分机管理", "串口设置", "探头登记"):
            navigate(label)
            assert window.title.text() == label
            views.append(page.pages.currentWidget())
        assert len(set(views)) == 3

        # This new board has no saved probes, despite the former local 165-point range.
        board = manager._channels[profile].simulator.board(target["address"])
        existing_probe = board.bindings[("ambient", 5)]
        board.bindings.clear()
        board.staged.clear()
        board.generation += 1
        assert controller.device(target_id)["registered_points"] is None

        submissions = []
        original_submit = manager.submit

        def record_submit(profile_id, request, expected, context=None):
            submissions.append((profile_id, bytes(request), expected.copy()))
            original_submit(profile_id, request, expected, context)

        monkeypatch.setattr(manager, "submit", record_submit)
        page.registration_device.setCurrentIndex(page.registration_device.findData(target_id))
        page.open_registration_button.click()
        wait_for(lambda: controller.device(target_id)["registered_points"] == [])
        dialog = page.registration_dialog
        assert dialog is not None and dialog.binding_table.rowCount() == 0
        assert profile in controller.paused

        dialog.close_button.click()
        wait_for(lambda: page.registration_dialog is None and profile not in controller.paused)
        wait_for(lambda: not manager.busy(profile))
        before = len(submissions)
        controller.poll(device_id=target_id)
        assert len(submissions) == before
        assert not any(cycle["device"]["id"] == target_id for cycle in controller.cycles.values())

        # Reopen after a verified nonempty board configuration appears, with no PC-side save.
        board.bindings[("ambient", 5)] = existing_probe
        board.staged = board.bindings.copy()
        board.generation += 1
        page.open_registration_button.click()
        wait_for(lambda: point_ids(controller.device(target_id)["registered_points"]) == {("ambient", 5)})
        dialog = page.registration_dialog
        dialog.scan_button.click()
        wait_for(lambda: dialog.probe_combo.count() > 3 and not dialog._busy)
        wait_for(lambda: not manager.busy(profile))
        before = len(submissions)
        for index, (kind, point) in enumerate((("grain", 12), ("grain", 160), ("ambient", 5)), 1):
            dialog.probe_combo.setCurrentIndex(index)
            dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData(kind))
            dialog.point_spin.setValue(point)
            dialog.bind_button.click()
        assert len(submissions) == before
        assert board.staged == board.bindings == {("ambient", 5): existing_probe}
        assert dialog.binding_table.rowCount() == 3
        assert dialog.binding_table.item(0, 3).text() == "待新增"
        assert dialog.binding_table.item(2, 3).text() == "待修改"

        dialog.save_button.click()
        expected_points = {("grain", 12), ("grain", 160), ("ambient", 5)}
        wait_for(lambda: point_ids(controller.device(target_id)["registered_points"]) == expected_points)
        assert set(board.bindings) == expected_points
        assert not dialog.dirty
        persisted = []
        window.repo.call("bindings", target_id, callback=persisted.append)
        wait_for(lambda: bool(persisted))
        assert point_ids(persisted[0]) == expected_points

        # Editing pauses one serial profile, while the other can still produce a new sample.
        assert controller.paused == {profile}
        wait_for(lambda: not manager.busy(other_profile))
        previous = controller.last_received[other_id]
        before = len(submissions)
        controller.poll()
        wait_for(lambda: controller.last_received[other_id] > previous)
        assert {item[0] for item in submissions[before:]} == {other_profile}

        navigate("串口设置")
        wait_for(lambda: page.registration_dialog is None and profile not in controller.paused)
        assert page.pages.currentWidget() is views[1]
        wait_for(lambda: not manager.busy(profile))
        before = len(submissions)
        controller.poll(device_id=target_id)
        wait_for(lambda: target_id in controller.latest)
        assert point_ids(controller.latest[target_id]) == expected_points
        assert [(expected["start"], expected["count"]) for sent_profile, _, expected in submissions[before:]
                if sent_profile == profile] == [(12, 1), (0x400B, 1), (160, 1), (0x409F, 1), (0x2008, 2), (0x40A9, 1)]
        assert not warnings, warnings
    finally:
        dialog = page.registration_dialog
        if dialog is not None and dialog._busy:
            if not dialog._writing:
                dialog.cancel_read()
            wait_for(lambda: not dialog._busy)
        window.close()
        wait_for(lambda: not window.repo.thread.isRunning())

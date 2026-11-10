"""Regression tests for parameter load guards and explicit history actions."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from warehouse_pc.app import MainWindow
from warehouse_pc.controller import Controller as AppController
from warehouse_pc.history_ui import HistoryPage
from warehouse_pc.maintenance_ui import MaintenancePage
from warehouse_pc.settings_ui import SettingsPage
from warehouse_pc.storage import Store


class DeferredRepository(QObject):
    ready = Signal()

    def __init__(self):
        super().__init__()
        self.thread = SimpleNamespace(isRunning=lambda: True)
        self.calls = []

    def call(self, method, *args, callback=None, on_error=None, **kwargs):
        self.calls.append((method, args, kwargs, callback, on_error))

    def respond(self, method, result=None, error=None, *, args=None):
        index = next(i for i, item in enumerate(self.calls)
                     if item[0] == method and (args is None or item[1] == args))
        call = self.calls.pop(index)
        if error is None:
            call[3](result)
        else:
            call[4](error)
        return call


class Controller(QObject):
    metadata_changed = Signal(object)
    alarms_changed = Signal(object)
    log = Signal(str)
    point_keys = staticmethod(AppController.point_keys)

    def __init__(self):
        super().__init__()
        self.metadata = {"devices": [
            {"id": 1, "name": "甲分机", "grain_count": 3, "ambient_count": 1, "address": 1},
            {"id": 2, "name": "乙分机", "grain_count": 5, "ambient_count": 0, "address": 2},
        ]}
        self.running = True
        self.demo = False
        self.running_changes = []
        self.intervals = []

    def refresh_alarms(self):
        pass

    def set_running(self, running):
        self.running_changes.append(running)
        self.running = running

    def set_intervals(self, poll, history, *, persist=True):
        self.intervals.append((poll, history, persist))

    def export_communication_diagnostics(self, destination):
        self.diagnostic_path = destination
        return 7


def test_parameter_loading_gates_editing_and_ignores_old_device_results(qt_application):
    repo = DeferredRepository()
    page = SettingsPage(repo, Controller())
    assert not page.temp_offset.isEnabled()
    assert not page.threshold_inputs["temp_high"].isEnabled()
    assert not page.intervals_button.isEnabled()
    page._save_calibration()
    page._save_threshold()
    page._save_intervals()
    assert all(not entry[0].startswith("save_") for entry in repo.calls)
    page.device_combo.setCurrentIndex(1)
    assert page.alarm_device_combo.currentData() == 2
    repo.respond("calibrations", [{"kind": "grain", "point": 1, "temp_offset": 9}], args=(1,))
    assert not page.temp_offset.isEnabled()
    repo.respond("calibrations", [{"kind": "grain", "point": 1, "temp_offset": 1.5}], args=(2,))
    assert page.temp_offset.isEnabled() and page.temp_offset.value() == 1.5
    with patch.object(QMessageBox, "warning"):
        repo.respond("thresholds", error="读取错误", args=(2,))
        repo.respond("get_setting", error="读取错误", args=("collection", {}))
    assert not page.threshold_inputs["temp_high"].isEnabled()
    assert not page.save_threshold_button.isEnabled()
    assert not page.intervals_button.isEnabled()
    page.deleteLater()


def test_unsaved_device_change_and_batch_calibration_confirm_affected_points(qt_application):
    repo = DeferredRepository()
    controller = Controller()
    controller.metadata["devices"][0].update(
        grain_count=9, bindings_verified=True,
        registered_points=[{"kind": "grain", "point": n} for n in (1, 3, 9)])
    page = SettingsPage(repo, controller)
    repo.respond("calibrations", [])
    repo.respond("thresholds", [])
    page.temp_offset.setValue(1.5)
    page.set_tab(2)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
        page.alarm_device_combo.setCurrentIndex(1)
    assert page.alarm_device_combo.currentData() == 1
    assert page.device_combo.currentData() == 1
    page.set_tab(1)
    assert page.temp_offset.value() == 1.5
    controller.metadata_changed.emit(controller.metadata)
    assert page.temp_offset.value() == 1.5
    page.batch_box.setChecked(True)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as confirm:
        page.save_calibration_button.click()
    assert "3 个" in confirm.call_args.args[2]
    assert not any(method == "save_calibrations" for method, *_ in repo.calls)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        page.save_calibration_button.click()
    call = repo.respond("save_calibrations", 1)
    assert call[1][0] == 1
    assert [row["point"] for row in call[1][1]] == [1, 3, 9]
    assert not page.temp_offset.isEnabled()
    repo.respond("calibrations", call[1][1])
    assert page.temp_offset.isEnabled() and page.temp_offset.value() == 1.5
    page.deleteLater()


def test_settings_tabs_keep_position_and_synchronize_the_shell(qt_application, tmp_path):
    window = MainWindow(tmp_path, demo=False)
    window.show()
    try:
        routes = {label: (button, group) for button, page, label, group in window.nav_routes if page == 3}
        alarm_button, _ = routes["报警记录"]
        alarm_button.click()
        qt_application.processEvents()
        page = window.settings
        bar = page._tabs.tabBar()
        start_y = bar.mapTo(page, QPoint()).y()
        assert page.current_tab() == 2 and window.title.text() == "报警记录"
        assert page.alarm_device_combo.isVisible() and not page.device_combo.isVisible()

        for index, title, visible_selector in (
            (0, "测温分机采集规则", None),
            (1, "温湿度微调", page.device_combo),
            (2, "报警记录", page.alarm_device_combo),
        ):
            QTest.mouseClick(bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(index).center())
            qt_application.processEvents()
            assert bar.mapTo(page, QPoint()).y() == start_y
            assert page.current_tab() == index and window.title.text() == title
            assert [button for button, _, _, _ in window.nav_routes if button.isChecked()] == [routes[title][0]]
            assert window.group_buttons[routes[title][1]].isChecked()
            assert page.device_combo.isVisible() == (visible_selector is page.device_combo)
            assert page.alarm_device_combo.isVisible() == (visible_selector is page.alarm_device_combo)

        collection_button, _ = routes["测温分机采集规则"]
        collection_button.click()
        assert page.current_tab() == 0 and window.title.text() == "测温分机采集规则"
    finally:
        window.close()
        for _ in range(200):
            if not window.repo.thread.isRunning():
                break
            QTest.qWait(5)
        assert not window.repo.thread.isRunning()


def test_global_intervals_apply_only_after_successful_write(qt_application):
    repo = DeferredRepository()
    controller = Controller()
    page = SettingsPage(repo, controller)
    repo.respond("get_setting", {"poll_seconds": 5, "history_seconds": 30}, args=("collection", {}))
    assert page.intervals_button.isEnabled() and controller.intervals == []
    page.poll_seconds.setValue(8)
    page.intervals_button.click()
    assert not page.intervals_button.isEnabled() and controller.intervals == []
    repo.respond("set_setting")
    assert controller.intervals == [(8, 30, False)] and page.intervals_button.isEnabled()
    page.deleteLater()


def test_history_views_share_applied_query_and_confirm_final_csv_path(qt_application, tmp_path):
    repo = DeferredRepository()
    controller = Controller()
    page = HistoryPage(repo, controller)
    page.refresh(controller.metadata, query=False)
    assert page.curve_area.isHidden() and not page.table_area.isHidden()
    page.device_combo.setCurrentIndex(1)
    page.query()
    assert not page.csv_button.isEnabled()
    sample = {"device_id": 1, "kind": "grain", "point": 3, "timestamp": "2026-11-10T10:00:00",
              "source": "serial", "status": "ok", "temp": 20}
    queried = repo.respond("history_count_filters", 1)[2]
    repo.respond("history_page", [sample])
    assert page.csv_button.isEnabled() and queried["device_id"] == 1
    page.set_view_mode("curve")
    assert page.table_area.isHidden() and not page.curve_area.isHidden()
    plotted = repo.respond("history_count_filters", 1)[2]
    repo.respond("history_page", [sample])
    assert plotted == {**queried, "kind": "grain", "point": 3}
    assert page.temperature_plot.listDataItems()
    page.set_view_mode("records")
    assert page.curve_area.isHidden() and not page.table_area.isHidden()
    page.device_combo.setCurrentIndex(2)
    destination = tmp_path / "records.csv"
    destination.write_text("existing", encoding="utf-8")
    with patch("warehouse_pc.history_ui.QFileDialog.getSaveFileName", return_value=(str(destination.with_suffix("")), "")):
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as confirm:
            page.export("csv")
        assert str(destination) in confirm.call_args.args[2]
        assert not any(method == "export_history" for method, *_ in repo.calls)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            page.export("csv")
    call = repo.respond("export_history", 0)
    assert call[1] == (str(destination),)
    assert call[2] == queried
    page.deleteLater()


def test_cleanup_preview_keeps_collection_running_and_delete_rechecks_count(qt_application, tmp_path):
    repo = DeferredRepository()
    controller = Controller()
    page = MaintenancePage(repo, controller, tmp_path)
    page.clear_history()
    assert controller.running and controller.running_changes == []
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
        preview = repo.respond("clear_history", 4)[2]
    assert preview["dry_run"] and controller.running_changes == []
    page.clear_history()

    def confirm(*args):
        assert controller.running
        assert args[-1] == QMessageBox.StandardButton.No
        return QMessageBox.StandardButton.Yes

    with patch.object(QMessageBox, "question", side_effect=confirm):
        repo.respond("clear_history", 4)
    assert controller.running_changes == [False]
    with patch.object(QMessageBox, "warning"):
        delete = repo.respond("clear_history", error="记录数已变化，请重新预览")[2]
    assert delete == {**preview, "dry_run": False, "expected_count": 4}
    assert controller.running_changes == [False, True]
    assert "重新预览" in page.status_label.text()
    page.deleteLater()


def test_maintenance_exports_communication_diagnostics(qt_application, tmp_path):
    controller = Controller()
    page = MaintenancePage(DeferredRepository(), controller, tmp_path)
    destination = str(tmp_path / "diagnostics.json")
    with patch("warehouse_pc.maintenance_ui.QFileDialog.getSaveFileName", return_value=(destination, "")):
        page.diagnostics_button.click()
    assert controller.diagnostic_path == destination
    assert "7 条通信记录" in page.status_label.text()
    page.deleteLater()


def test_changed_history_count_prevents_deletion(tmp_path):
    store = Store(tmp_path / "history.db")
    try:
        warehouse = store.save_warehouse("仓房")
        port = store.save_port("串口", "COM1")
        device = store.save_device("分机", warehouse, port)
        sample = {"kind": "grain", "point": 1, "temperature": 20, "status": 0}
        filters = {"start": "2026-11-10T00:00:00", "end": "2026-11-10T23:59:59", "device_id": device}
        store.save_samples(device, [sample], timestamp="2026-11-10T12:00:00")
        preview = store.clear_history(**filters, dry_run=True)
        store.save_samples(device, [sample], timestamp="2026-11-10T12:01:00")
        with pytest.raises(ValueError, match="重新预览"):
            store.clear_history(**filters, expected_count=preview)
        assert store.history_count(device_id=device) == 2
    finally:
        store.close()

"""Local backups, configuration transfer, history cleanup and bounded logs.

All database operations use Repository; this page never opens SQLite itself.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDateTime, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QDateTimeEdit, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from .domain import source_for_port


def configuration_source(data: Any, expected: str) -> None:
    """Reject mixed simulator/serial manifests before transaction import."""
    if not isinstance(data, dict) or data.get("format") != "warehouse-pc" or data.get("version") != 1:
        raise ValueError("配置文件格式或版本不受支持")
    allowed = {"simulator", "serial"}
    if expected not in allowed:
        raise ValueError("当前数据模式无效")
    declared = data.get("source")
    if declared is not None and declared != expected:
        raise ValueError("配置来源与当前模拟/真实数据模式不同")
    if "demo" in data and (not isinstance(data["demo"], bool) or data["demo"] != (expected == "simulator")):
        raise ValueError("配置文件的演示标记与当前模式不同")
    for field in ("warehouses", "ports", "devices", "calibrations", "thresholds", "bindings"):
        if not isinstance(data.get(field, []), list):
            raise ValueError(f"配置 {field} 必须是列表")
    ports, devices = data.get("ports"), data.get("devices")
    if not isinstance(ports, list) or not isinstance(devices, list):
        raise ValueError("配置文件缺少串口或设备列表")
    port_sources: dict[int, str] = {}
    for port in ports:
        if not isinstance(port, dict) or not isinstance(port.get("port_name"), str):
            raise ValueError("配置串口资料不完整")
        identifier = port.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 1 or identifier in port_sources:
            raise ValueError("配置串口编号无效或重复")
        source = source_for_port(port["port_name"])
        if source != expected or port.get("source", source) != source:
            raise ValueError("配置包含其他模式的串口，不能混用模拟与真实档案")
        port_sources[identifier] = source
    for device in devices:
        if not isinstance(device, dict) or device.get("port_id") not in port_sources:
            raise ValueError("配置设备引用了不存在的串口")
        source = port_sources[device["port_id"]]
        if device.get("source", source) != source:
            raise ValueError("配置设备来源与串口不一致")
    # An empty legacy manifest cannot prove which mode produced its settings.
    if not ports and declared is None and "demo" not in data:
        raise ValueError("空配置缺少来源标记，无法确认模拟/真实数据模式")


class MaintenancePage(QWidget):
    busy_changed = Signal(bool)

    def __init__(self, repository: Any, controller: Any, data_dir: Path,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.repository, self.controller = repository, controller
        self.data_dir = Path(data_dir)
        self.is_demo = bool(controller.demo)
        self.source = "simulator" if self.is_demo else "serial"
        worker = getattr(repository, "worker", None)
        self.db_path = Path(getattr(repository, "path", getattr(worker, "path", self.data_dir / "warehouse.sqlite3")))
        self._busy = False
        self._resume_running = False
        self._build_ui()
        controller.log.connect(self.append_log)
        controller.metadata_changed.connect(self._metadata)
        self._metadata(controller.metadata)
        self._space_timer = QTimer(self)
        self._space_timer.setInterval(30000)
        self._space_timer.timeout.connect(self.refresh_disk)
        self._space_timer.start()
        self.refresh_disk()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        heading = QLabel("本地维护")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        mode = QLabel("模拟数据库 · 与真实设备数据隔离" if self.is_demo else "真实设备数据库 · Windows 离线串口采集")
        mode.setWordWrap(True)
        layout.addWidget(mode)
        self.path_label = QLabel(f"数据库：{self.db_path}\n数据目录：{self.data_dir}")
        self.path_label.setWordWrap(True)
        layout.addWidget(self.path_label)
        disk_row = QHBoxLayout()
        self.space_label = QLabel()
        disk_row.addWidget(self.space_label, 1)
        self.folder_button = QPushButton("打开数据目录")
        self.folder_button.clicked.connect(self.open_data_directory)
        disk_row.addWidget(self.folder_button)
        layout.addLayout(disk_row)

        transfer_box = QGroupBox("备份与配置迁移")
        transfer_layout = QVBoxLayout(transfer_box)
        note = QLabel("数据库备份包含全部档案、采集历史及报警；JSON 配置仅迁移档案与设置，事务合并并保留已有历史。")
        note.setWordWrap(True)
        transfer_layout.addWidget(note)
        transfer_row = QHBoxLayout()
        self.backup_button = QPushButton("备份完整数据库…")
        self.restore_button = QPushButton("恢复完整数据库…")
        self.export_button = QPushButton("导出 JSON 配置…")
        self.import_button = QPushButton("导入 JSON 配置…")
        self.backup_button.clicked.connect(self.backup_database)
        self.restore_button.clicked.connect(self.restore_database)
        self.export_button.clicked.connect(self.export_configuration)
        self.import_button.clicked.connect(self.import_configuration)
        for button in (self.backup_button, self.restore_button, self.export_button, self.import_button):
            button.setMaximumWidth(170)
            transfer_row.addWidget(button)
        transfer_row.addStretch()
        transfer_layout.addLayout(transfer_row)
        layout.addWidget(transfer_box)

        cleanup_box = QGroupBox("按时间清理采集历史")
        cleanup_layout = QVBoxLayout(cleanup_box)
        cleanup_note = QLabel("预览时继续采集，确认后暂停并核对记录数。仅清理采集记录，保留档案、设置和报警记录。")
        cleanup_note.setWordWrap(True)
        cleanup_layout.addWidget(cleanup_note)
        form = QFormLayout()
        self.device_combo = QComboBox()
        self.device_combo.addItem("全部分机", None)
        self.device_combo.setMaximumWidth(300)
        self.start_date, self.end_date = QDateTimeEdit(), QDateTimeEdit()
        for widget in (self.start_date, self.end_date):
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            widget.setMaximumWidth(215)
        self.start_date.setDateTime(QDateTime.currentDateTime().addMonths(-1))
        self.end_date.setDateTime(QDateTime.currentDateTime())
        form.addRow("分机范围", self.device_combo)
        dates = QHBoxLayout()
        dates.addWidget(self.start_date)
        dates.addWidget(QLabel("至"))
        dates.addWidget(self.end_date)
        self.cleanup_button = QPushButton("统计并确认清理…")
        self.cleanup_button.clicked.connect(self.clear_history)
        dates.addWidget(self.cleanup_button)
        dates.addStretch()
        form.addRow("时间范围（含起止）", dates)
        cleanup_layout.addLayout(form)
        layout.addWidget(cleanup_box)

        log_row = QHBoxLayout()
        log_row.addWidget(QLabel("运行日志（最多显示 1000 行）"), 1)
        self.diagnostics_button = QPushButton("导出通信诊断…")
        self.diagnostics_button.clicked.connect(self.export_communication_diagnostics)
        log_row.addWidget(self.diagnostics_button)
        layout.addLayout(log_row)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(1000)
        self.log_view.setMinimumHeight(100)
        layout.addWidget(self.log_view, 1)
        self.status_label = QLabel("就绪")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self._actions = [self.backup_button, self.restore_button, self.export_button,
                         self.import_button, self.cleanup_button, self.diagnostics_button,
                         self.device_combo, self.start_date, self.end_date]

    def append_log(self, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{timestamp}  {text}")

    def _metadata(self, metadata: dict) -> None:
        current = self.device_combo.currentData()
        self.device_combo.clear()
        self.device_combo.addItem("全部分机", None)
        for device in metadata.get("devices", []):
            self.device_combo.addItem(f"{device['name']}（地址 {device['address']}）", device["id"])
        index = self.device_combo.findData(current)
        self.device_combo.setCurrentIndex(max(0, index))

    def refresh_disk(self) -> None:
        try:
            directory = self.data_dir
            while not directory.exists() and directory != directory.parent:
                directory = directory.parent
            usage = shutil.disk_usage(directory)
            size = self.db_path.stat().st_size if self.db_path.exists() else 0
            self.space_label.setText(f"磁盘可用 {usage.free / 1024 ** 3:.1f} GB / 共 {usage.total / 1024 ** 3:.1f} GB"
                                     f" ｜ 数据库文件 {size / 1024 ** 2:.1f} MB（不含 WAL）")
        except OSError as exc:
            self.space_label.setText(f"暂无法读取磁盘状态：{exc}")

    def open_data_directory(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir.resolve()))):
                raise OSError("系统未能打开目录")
        except OSError as exc:
            self._error(f"无法打开数据目录：{exc}")

    def export_communication_diagnostics(self) -> None:
        if self._busy:
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "导出通信诊断", str(self.data_dir / f"通信诊断_{datetime.now():%Y%m%d_%H%M%S}.json"),
            "JSON 诊断文件 (*.json)")
        if not filename:
            return
        try:
            if Path(filename).resolve() == self.db_path.resolve():
                raise ValueError("诊断文件不能覆盖正在使用的数据库")
            count = self.controller.export_communication_diagnostics(filename)
        except (OSError, ValueError) as exc:
            self._error(f"未导出通信诊断：{exc}")
            return
        self._finish(f"已导出通信诊断（{count} 条通信记录）：{filename}")

    def _set_busy(self, value: bool, text: str = "") -> None:
        changed = self._busy != value
        self._busy = value
        for action in self._actions:
            action.setEnabled(not value)
        if changed:
            self.busy_changed.emit(value)
        if text:
            self.status_label.setText(text)

    def _finish(self, text: str) -> None:
        self._set_busy(False, text)
        self.append_log(text)
        self.refresh_disk()

    def _error(self, text: str) -> None:
        self._resume_after_cleanup()
        self._finish(text)
        QMessageBox.warning(self, "维护操作未完成", text)

    def _call(self, method: str, *args: Any, callback: Any,
              on_error: Any = None, **kwargs: Any) -> None:
        handler = on_error or self._error
        try:
            self.repository.call(method, *args, callback=callback, on_error=handler, **kwargs)
        except Exception as exc:
            handler(str(exc))

    def _default_backup(self, prefix: str = "数据库备份") -> str:
        return str(self.data_dir / "backups" / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.db")

    def backup_database(self) -> None:
        if self._busy:
            return
        filename, _ = QFileDialog.getSaveFileName(self, "备份全部档案和历史", self._default_backup(), "SQLite 数据库 (*.db *.sqlite3)")
        if not filename:
            return
        if Path(filename).resolve() == self.db_path.resolve():
            self._error("备份目标不能是正在使用的数据库")
            return
        self._set_busy(True, "正在创建一致性数据库快照…")
        self._call("backup", filename, callback=lambda _: self._finish(f"完整数据库已备份：{filename}"))

    def _clear_controller_cache(self) -> None:
        for name in ("latest", "last_stored", "last_received", "cycles", "processing_devices"):
            value = getattr(self.controller, name, None)
            if hasattr(value, "clear"):
                value.clear()

    def _reload_after_change(self, text: str) -> None:
        def loaded(settings: Any) -> None:
            settings = settings if isinstance(settings, dict) else {}
            poll, history = settings.get("poll_seconds", 5), settings.get("history_seconds", 30)
            if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not 1 <= poll <= 86400:
                poll = 5
            if isinstance(history, bool) or not isinstance(history, (int, float)) or not 1 <= history <= 86400 * 365:
                history = 30
            self.controller.set_intervals(int(poll), int(history))

            def metadata_ready(_: Any) -> None:
                self.controller.refresh_alarms()
                self._finish(text + "；采集保持暂停，请核对档案后手动连接和启动")
            self.controller.reload_metadata(callback=metadata_ready,
                on_error=lambda error: self._error("配置已更新，但界面资料刷新失败，请重启后核对。\n" + error))
        self._call("get_setting", "collection", {}, callback=loaded)

    def restore_database(self) -> None:
        if self._busy:
            return
        filename, _ = QFileDialog.getOpenFileName(self, "选择要恢复的完整数据库", str(self.data_dir), "SQLite 数据库 (*.db *.sqlite3);;所有文件 (*)")
        if not filename:
            return
        source = Path(filename)
        try:
            if not source.is_file() or source.stat().st_size == 0:
                raise ValueError("选择的备份文件不存在或为空")
            if source.resolve() == self.db_path.resolve():
                raise ValueError("不能使用正在运行的数据库作为恢复来源")
        except (OSError, ValueError) as exc:
            self._error(str(exc))
            return
        phrase = "恢复全部数据库"
        typed, accepted = QInputDialog.getText(
            self, "确认覆盖全部档案与历史", f"备份文件：{source.resolve()}\n"
            f"目标数据库：{self.db_path.resolve()}\n"
            "恢复将覆盖当前全部档案、采集历史、报警和设置。\n"
            "先暂停采集并断开串口，自动保存当前数据库安全副本，再执行恢复。\n"
            f"确认后输入：{phrase}")
        if not accepted or typed.strip() != phrase:
            return
        self._set_busy(True, "已暂停采集，正在备份当前数据库以便恢复失败时回退…")
        self.controller.set_running(False)
        self.controller.disconnect_all()
        self._clear_controller_cache()
        safety = self.data_dir / "safety" / f"恢复前_{datetime.now():%Y%m%d_%H%M%S_%f}.db"

        def safety_ready(_: Any) -> None:
            self.status_label.setText("安全副本已保存，正在校验并恢复所选数据库…")

            def restored(_: Any) -> None:
                self._clear_controller_cache()
                self._reload_after_change(f"数据库恢复完成；恢复前安全副本：{safety}")

            def restore_error(message: str) -> None:
                self._clear_controller_cache()
                self.controller.reload_metadata()
                self.controller.refresh_alarms()
                self._error(f"数据库恢复未完成：{message}\n恢复前安全副本：{safety}\n采集保持暂停。")
            self._call("restore_backup", str(source), expected_source=self.source,
                       callback=restored, on_error=restore_error)
        self._call("backup", str(safety), callback=safety_ready)

    def export_configuration(self) -> None:
        if self._busy:
            return
        filename, _ = QFileDialog.getSaveFileName(self, "导出档案和设置（不含历史）",
            str(self.data_dir / f"配置_{datetime.now():%Y%m%d_%H%M%S}.json"), "JSON (*.json)")
        if not filename:
            return
        if Path(filename).resolve() == self.db_path.resolve():
            self._error("JSON 配置不能覆盖正在使用的数据库")
            return
        self._set_busy(True, "正在读取配置…")

        def received(document: dict) -> None:
            document = dict(document)
            document["source"], document["demo"] = self.source, self.is_demo
            try:
                configuration_source(document, self.source)
                destination = Path(filename)
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(destination.name + ".tmp")
                temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
                temporary.replace(destination)
            except (OSError, ValueError, TypeError) as exc:
                self._error(f"配置未导出成功：{exc}")
                return
            self._finish(f"配置已导出（不含历史）：{filename}")
        self._call("export_config", callback=received)

    def import_configuration(self) -> None:
        if self._busy:
            return
        filename, _ = QFileDialog.getOpenFileName(self, "导入 JSON 配置", str(self.data_dir), "JSON (*.json)")
        if not filename:
            return
        try:
            path = Path(filename)
            if path.stat().st_size > 20_000_000:
                raise ValueError("配置文件过大")
            document = json.loads(path.read_text(encoding="utf-8"))
            configuration_source(document, self.source)
        except (OSError, ValueError, TypeError) as exc:
            self._error(f"未导入配置：{exc}")
            return
        answer = QMessageBox.question(self, "确认合并配置",
            f"文件：{Path(filename).resolve()}\n仓房 {len(document.get('warehouses', []))} 个，"
            f"串口 {len(document['ports'])} 个，分机 {len(document['devices'])} 台。\n"
            "按仓房/串口/设备端点合并配置；同名或同端点设置可能更新，已有采集历史和无关档案保留。\n"
            "采集将暂停、串口断开，完成后请核对配置再手动启动。继续导入？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True, "正在事务合并配置，保留已有历史…")
        self.controller.set_running(False)
        self.controller.disconnect_all()
        self._clear_controller_cache()
        self._call("import_config", document, callback=lambda _: self._reload_after_change("配置导入完成，已有历史保留"))

    def _resume_after_cleanup(self) -> None:
        if self._resume_running:
            self._resume_running = False
            self.controller.set_running(True)

    def clear_history(self) -> None:
        if self._busy:
            return
        start = self.start_date.dateTime().toString("yyyy-MM-dd'T'HH:mm:ss")
        end = self.end_date.dateTime().toString("yyyy-MM-dd'T'HH:mm:ss")
        if start > end:
            self._error("开始时间不能晚于结束时间")
            return
        identifier = self.device_combo.currentData()
        name = self.device_combo.currentText()
        self._set_busy(True, "正在统计待清理采集记录，采集继续运行…")

        def counted(count: int) -> None:
            if count == 0:
                self._finish("所选范围没有采集记录，未删除任何内容")
                return
            answer = QMessageBox.question(self, "确认删除采集历史",
                f"分机：{name}\n范围：{start} 至 {end}（含起止）\n"
                f"预统计 {count:,} 条采集记录，将永久删除这些范围内的采集记录。\n"
                "确认后会暂停采集并再次核对记录数。仓房、分机、登记、修正值和报警记录保留。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                self._finish("已取消清理，历史记录未修改")
                return
            self._resume_running = bool(self.controller.running)
            if self._resume_running:
                self.controller.set_running(False)
            self.status_label.setText("正在核对并清理已确认范围的采集历史…")

            def deleted(actual: int) -> None:
                self.controller.refresh_alarms()
                self._resume_after_cleanup()
                self._finish(f"已清理 {actual:,} 条采集历史；档案、设置及报警记录保留")
            self._call("clear_history", start=start, end=end, device_id=identifier,
                       dry_run=False, expected_count=count, callback=deleted)
        self._call("clear_history", start=start, end=end, device_id=identifier,
                   dry_run=True, callback=counted)

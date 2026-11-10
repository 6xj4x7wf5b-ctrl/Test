"""Local backups, configuration transfer, history cleanup and bounded logs.

All database operations use Repository; this page never opens SQLite itself.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDateTime, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QDateTimeEdit, QFileDialog, QHBoxLayout,
    QInputDialog, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
    QToolButton, QVBoxLayout, QWidget,
)

from .domain import source_for_port
from .filter_panel import FilterPanel
from .i18n import tr


def configuration_source(data: Any, expected: str) -> None:
    """Reject mixed simulator/serial manifests before transaction import."""
    if not isinstance(data, dict) or data.get("format") != "warehouse-pc" or data.get("version") != 1:
        raise ValueError(tr("配置文件格式或版本不受支持"))
    allowed = {"simulator", "serial"}
    if expected not in allowed:
        raise ValueError(tr("当前数据模式无效"))
    declared = data.get("source")
    if declared is not None and declared != expected:
        raise ValueError(tr("配置来源与当前模拟/真实数据模式不同"))
    if "demo" in data and (not isinstance(data["demo"], bool) or data["demo"] != (expected == "simulator")):
        raise ValueError(tr("配置文件的演示标记与当前模式不同"))
    for field in ("warehouses", "ports", "devices", "calibrations", "thresholds", "bindings"):
        if not isinstance(data.get(field, []), list):
            raise ValueError(tr('配置 {field} 必须是列表', field=field))
    ports, devices = data.get("ports"), data.get("devices")
    if not isinstance(ports, list) or not isinstance(devices, list):
        raise ValueError(tr("配置文件缺少串口或设备列表"))
    port_sources: dict[int, str] = {}
    for port in ports:
        if not isinstance(port, dict) or not isinstance(port.get("port_name"), str):
            raise ValueError(tr("配置串口资料不完整"))
        identifier = port.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 1 or identifier in port_sources:
            raise ValueError(tr("配置串口编号无效或重复"))
        source = source_for_port(port["port_name"])
        if source != expected or port.get("source", source) != source:
            raise ValueError(tr("配置包含其他模式的串口，不能混用模拟与真实档案"))
        port_sources[identifier] = source
    for device in devices:
        if not isinstance(device, dict) or device.get("port_id") not in port_sources:
            raise ValueError(tr("配置设备引用了不存在的串口"))
        source = port_sources[device["port_id"]]
        if device.get("source", source) != source:
            raise ValueError(tr("配置设备来源与串口不一致"))
    # An empty legacy manifest cannot prove which mode produced its settings.
    if not ports and declared is None and "demo" not in data:
        raise ValueError(tr("空配置缺少来源标记，无法确认模拟/真实数据模式"))


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
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        heading = QLabel(tr("数据库备份"))
        heading.setObjectName("sectionTitle")
        backup_row = QHBoxLayout()
        backup_row.addWidget(heading)
        backup_row.addStretch()
        self.backup_button = QPushButton(tr("备份数据库…"))
        self.backup_button.setObjectName("primary")
        self.backup_button.setToolTip(tr("备份全部档案、历史、报警和设置"))
        self.backup_button.clicked.connect(self.backup_database)
        backup_row.addWidget(self.backup_button)
        layout.addLayout(backup_row)
        self.space_label = QLabel()
        self.space_label.setObjectName("muted")
        layout.addWidget(self.space_label)

        cleanup_heading = QLabel(tr("历史清理"))
        cleanup_heading.setObjectName("sectionTitle")
        layout.addWidget(cleanup_heading)
        cleanup_note = QLabel(tr("仅删除采集记录，保留档案、设置和报警。先预览，再确认。"))
        cleanup_note.setObjectName("muted")
        cleanup_note.setWordWrap(True)
        layout.addWidget(cleanup_note)
        cleanup_filters = FilterPanel()
        self.device_combo = QComboBox()
        self.device_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.device_combo.setMinimumContentsLength(8)
        self.device_combo.addItem(tr("全部分机"), None)
        self.start_date, self.end_date = QDateTimeEdit(), QDateTimeEdit()
        for widget in (self.start_date, self.end_date):
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.start_date.setDateTime(QDateTime.currentDateTime().addMonths(-1))
        self.end_date.setDateTime(QDateTime.currentDateTime())
        cleanup_filters.add_field(tr("分机"), self.device_combo)
        cleanup_filters.add_field(tr("开始时间"), self.start_date)
        cleanup_filters.add_field(tr("结束时间"), self.end_date)
        self.cleanup_button = QPushButton(tr("预览清理…"))
        self.cleanup_button.setObjectName("secondary")
        self.cleanup_button.clicked.connect(self.clear_history)
        cleanup_filters.add_action(self.cleanup_button)
        layout.addWidget(cleanup_filters)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        advanced = QWidget()
        advanced_layout = QVBoxLayout(advanced)
        advanced_layout.setContentsMargins(8, 0, 0, 8)
        path_label = tr("演示数据库：{path}", path=self.db_path) if self.is_demo else tr("实机数据库：{path}", path=self.db_path)
        self.path_label = QLabel(path_label)
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        advanced_layout.addWidget(self.path_label)
        transfer_row = QHBoxLayout()
        self.restore_button = QPushButton(tr("恢复数据库…"))
        self.restore_button.setToolTip(tr("恢复将覆盖档案、历史、报警和设置，操作前保存安全副本"))
        self.export_button = QPushButton(tr("导出配置…"))
        self.import_button = QPushButton(tr("导入配置…"))
        self.import_button.setToolTip(tr("仅合并档案和设置，保留已有采集历史"))
        self.export_button.setToolTip(tr("导出 JSON 档案和设置，不含历史"))
        self.folder_button = QPushButton(tr("打开数据目录"))
        self.folder_button.setToolTip(str(self.data_dir))
        for button, handler in (
            (self.restore_button, self.restore_database),
            (self.export_button, self.export_configuration),
            (self.import_button, self.import_configuration),
            (self.folder_button, self.open_data_directory),
        ):
            button.setObjectName("quietButton")
            button.clicked.connect(handler)
            transfer_row.addWidget(button)
        transfer_row.addStretch()
        advanced_layout.addLayout(transfer_row)
        self.advanced_toggle = self._fold_button(tr("高级维护"), advanced)
        layout.addWidget(self.advanced_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(advanced)

        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(8, 0, 0, 0)
        self.diagnostics_button = QPushButton(tr("导出通信诊断…"))
        self.diagnostics_button.setObjectName("quietButton")
        self.diagnostics_button.clicked.connect(self.export_communication_diagnostics)
        log_layout.addWidget(self.diagnostics_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setToolTip(tr("最多显示最近 1000 行"))
        self.log_view.document().setMaximumBlockCount(1000)
        self.log_view.setMinimumHeight(125)
        log_layout.addWidget(self.log_view, 1)
        self.log_toggle = self._fold_button(tr("通信日志"), log_panel)
        layout.addWidget(self.log_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(log_panel, 1)
        layout.addStretch()

        self._actions = [self.backup_button, self.restore_button, self.export_button,
                         self.import_button, self.cleanup_button, self.diagnostics_button,
                         self.device_combo, self.start_date, self.end_date]

    @staticmethod
    def _fold_button(label, panel):
        button = QToolButton()
        button.setObjectName("quietButton")
        button.setText(label)
        button.setCheckable(True)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setArrowType(Qt.ArrowType.RightArrow)
        panel.hide()

        def toggle(visible):
            panel.setVisible(visible)
            button.setArrowType(Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow)

        button.toggled.connect(toggle)
        return button

    def append_log(self, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{timestamp}  {text}")

    def _metadata(self, metadata: dict) -> None:
        current = self.device_combo.currentData()
        self.device_combo.clear()
        self.device_combo.addItem(tr("全部分机"), None)
        for device in metadata.get("devices", []):
            self.device_combo.addItem(tr('{name}（地址 {address}）', name=device['name'], address=device['address']), device["id"])
        index = self.device_combo.findData(current)
        self.device_combo.setCurrentIndex(max(0, index))

    def refresh_disk(self) -> None:
        try:
            directory = self.data_dir
            while not directory.exists() and directory != directory.parent:
                directory = directory.parent
            usage = shutil.disk_usage(directory)
            size = self.db_path.stat().st_size if self.db_path.exists() else 0
            self.space_label.setText(tr('磁盘可用 {free:.1f} GB · 数据库 {size:.1f} MB', free=usage.free / 1024 ** 3, size=size / 1024 ** 2))
            self.space_label.setToolTip(tr('磁盘总容量 {total:.1f} GB，数据库大小不含 WAL。', total=usage.total / 1024 ** 3))
        except OSError as exc:
            self.space_label.setText(tr('暂无法读取磁盘状态：{error}', error=exc))

    def open_data_directory(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir.resolve()))):
                raise OSError(tr("系统未能打开目录"))
        except OSError as exc:
            self._error(tr('无法打开数据目录：{error}', error=exc))

    def export_communication_diagnostics(self) -> None:
        if self._busy:
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, tr("导出通信诊断"), str(self.data_dir / tr('通信诊断_{stamp:%Y%m%d_%H%M%S}.json', stamp=datetime.now())),
            tr("JSON 诊断文件 (*.json)"))
        if not filename:
            return
        try:
            if Path(filename).resolve() == self.db_path.resolve():
                raise ValueError(tr("诊断文件不能覆盖正在使用的数据库"))
            count = self.controller.export_communication_diagnostics(filename)
        except (OSError, ValueError) as exc:
            self._error(tr('未导出通信诊断：{error}', error=exc))
            return
        self._finish(tr('已导出通信诊断（{count} 条通信记录）：{filename}', count=count, filename=filename))

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
        QMessageBox.warning(self, tr("维护操作未完成"), text)

    def _call(self, method: str, *args: Any, callback: Any,
              on_error: Any = None, **kwargs: Any) -> None:
        handler = on_error or self._error
        try:
            self.repository.call(method, *args, callback=callback, on_error=handler, **kwargs)
        except Exception as exc:
            handler(str(exc))

    def _default_backup(self) -> str:
        return str(self.data_dir / "backups" / tr("数据库备份_{stamp}.db", stamp=datetime.now().strftime("%Y%m%d_%H%M%S")))

    def backup_database(self) -> None:
        if self._busy:
            return
        filename, _ = QFileDialog.getSaveFileName(self, tr("备份全部档案和历史"), self._default_backup(), tr("SQLite 数据库 (*.db *.sqlite3)"))
        if not filename:
            return
        if Path(filename).resolve() == self.db_path.resolve():
            self._error(tr("备份目标不能是正在使用的数据库"))
            return
        self._set_busy(True, tr("正在创建一致性数据库快照…"))
        self._call("backup", filename, callback=lambda _: self._finish(tr('完整数据库已备份：{filename}', filename=filename)))

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
                self._finish(tr("{result}；采集保持暂停，请核对档案后手动连接和启动", result=text))
            self.controller.reload_metadata(callback=metadata_ready,
                on_error=lambda error: self._error(tr("配置已更新，但界面资料刷新失败，请重启后核对。\n") + error))
        self._call("get_setting", "collection", {}, callback=loaded)

    def restore_database(self) -> None:
        if self._busy:
            return
        filename, _ = QFileDialog.getOpenFileName(self, tr("选择要恢复的完整数据库"), str(self.data_dir), tr("SQLite 数据库 (*.db *.sqlite3);;所有文件 (*)"))
        if not filename:
            return
        source = Path(filename)
        try:
            if not source.is_file() or source.stat().st_size == 0:
                raise ValueError(tr("选择的备份文件不存在或为空"))
            if source.resolve() == self.db_path.resolve():
                raise ValueError(tr("不能使用正在运行的数据库作为恢复来源"))
        except (OSError, ValueError) as exc:
            self._error(str(exc))
            return
        phrase = tr("恢复全部数据库")
        typed, accepted = QInputDialog.getText(
            self, tr("确认覆盖全部档案与历史"),
            tr("备份文件：{backup_path}\n目标数据库：{target}\n"
               "恢复将覆盖当前全部档案、采集历史、报警和设置。\n"
               "先暂停采集并断开串口，自动保存当前数据库安全副本，再执行恢复。\n"
               "确认后输入：{phrase}",
               backup_path=source.resolve(), target=self.db_path.resolve(), phrase=phrase))
        if not accepted or typed.strip() != phrase:
            return
        self._set_busy(True, tr("已暂停采集，正在备份当前数据库以便恢复失败时回退…"))
        self.controller.set_running(False)
        self.controller.disconnect_all()
        self._clear_controller_cache()
        safety = self.data_dir / "safety" / tr('恢复前_{stamp:%Y%m%d_%H%M%S_%f}.db', stamp=datetime.now())

        def safety_ready(_: Any) -> None:
            self.status_label.setText(tr("安全副本已保存，正在校验并恢复所选数据库…"))

            def restored(_: Any) -> None:
                self._clear_controller_cache()
                self._reload_after_change(tr('数据库恢复完成；恢复前安全副本：{safety}', safety=safety))

            def restore_error(message: str) -> None:
                self._clear_controller_cache()
                self.controller.reload_metadata()
                self.controller.refresh_alarms()
                self._error(tr('数据库恢复未完成：{message}\n恢复前安全副本：{safety}\n采集保持暂停。', message=message, safety=safety))
            self._call("restore_backup", str(source), expected_source=self.source,
                       callback=restored, on_error=restore_error)
        self._call("backup", str(safety), callback=safety_ready)

    def export_configuration(self) -> None:
        if self._busy:
            return
        filename, _ = QFileDialog.getSaveFileName(self, tr("导出档案和设置（不含历史）"),
            str(self.data_dir / tr('配置_{stamp:%Y%m%d_%H%M%S}.json', stamp=datetime.now())), "JSON (*.json)")
        if not filename:
            return
        if Path(filename).resolve() == self.db_path.resolve():
            self._error(tr("JSON 配置不能覆盖正在使用的数据库"))
            return
        self._set_busy(True, tr("正在读取配置…"))

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
                self._error(tr('配置未导出成功：{error}', error=exc))
                return
            self._finish(tr('配置已导出（不含历史）：{filename}', filename=filename))
        self._call("export_config", callback=received)

    def import_configuration(self) -> None:
        if self._busy:
            return
        filename, _ = QFileDialog.getOpenFileName(self, tr("导入 JSON 配置"), str(self.data_dir), "JSON (*.json)")
        if not filename:
            return
        try:
            path = Path(filename)
            if path.stat().st_size > 20_000_000:
                raise ValueError(tr("配置文件过大"))
            document = json.loads(path.read_text(encoding="utf-8"))
            configuration_source(document, self.source)
        except (OSError, ValueError, TypeError) as exc:
            self._error(tr('未导入配置：{error}', error=exc))
            return
        answer = QMessageBox.question(self, tr("确认合并配置"),
            tr("文件：{path}\n仓房 {warehouses} 个，串口 {ports} 个，分机 {devices} 台。\n"
               "按仓房/串口/设备端点合并配置；同名或同端点设置可能更新，已有采集历史和无关档案保留。\n"
               "采集将暂停、串口断开，完成后请核对配置再手动启动。继续导入？",
               path=Path(filename).resolve(), warehouses=len(document.get("warehouses", [])),
               ports=len(document["ports"]), devices=len(document["devices"])),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True, tr("正在事务合并配置，保留已有历史…"))
        self.controller.set_running(False)
        self.controller.disconnect_all()
        self._clear_controller_cache()
        self._call("import_config", document, callback=lambda _: self._reload_after_change(tr("配置导入完成，已有历史保留")))

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
            self._error(tr("开始时间不能晚于结束时间"))
            return
        identifier = self.device_combo.currentData()
        name = self.device_combo.currentText()
        self._set_busy(True, tr("正在统计待清理采集记录，采集继续运行…"))

        def counted(count: int) -> None:
            if count == 0:
                self._finish(tr("所选范围没有采集记录，未删除任何内容"))
                return
            answer = QMessageBox.question(self, tr("确认删除采集历史"),
                tr("分机：{name}\n范围：{start} 至 {end}（含起止）\n"
                   "预统计 {count:,} 条采集记录，将永久删除这些范围内的采集记录。\n"
                   "确认后会暂停采集并再次核对记录数。仓房、分机、登记、修正值和报警记录保留。是否继续？",
                   name=name, start=start, end=end, count=count),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                self._finish(tr("已取消清理，历史记录未修改"))
                return
            self._resume_running = bool(self.controller.running)
            if self._resume_running:
                self.controller.set_running(False)
            self.status_label.setText(tr("正在核对并清理已确认范围的采集历史…"))

            def deleted(actual: int) -> None:
                self.controller.refresh_alarms()
                self._resume_after_cleanup()
                self._finish(tr('已清理 {actual:,} 条采集历史；档案、设置及报警记录保留', actual=actual))
            self._call("clear_history", start=start, end=end, device_id=identifier,
                       dry_run=False, expected_count=count, callback=deleted)
        self._call("clear_history", start=start, end=end, device_id=identifier,
                   dry_run=True, callback=counted)

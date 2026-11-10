from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys

from PySide6.QtCore import QDateTime, QLockFile, QStandardPaths, QTimer, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget, QScrollArea, QLayout,
)

from . import __version__
from .controller import Controller
from .devices_ui import DevicesPage, button
from .history_ui import HistoryPage
from .live_ui import LivePage
from .maintenance_ui import MaintenancePage
from .repository import Repository
from .settings_ui import SettingsPage
from .style import STYLE
from .overview_ui import OverviewPage
from .warehouse_ui import WarehousePage
from .web_icons import navigation_icon
from .i18n import current_language, load_preference, set_language, tr
from .language_ui import FirstLaunchLanguageDialog, LanguagePage


SETTINGS_TAB_TITLES = ("测温分机采集规则", "温湿度微调", "报警记录")


class ActivePageStack(QStackedWidget):
    """Let the scroll area measure this page instead of the largest hidden page."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout().setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.currentChanged.connect(lambda _: self.updateGeometry())

    def sizeHint(self):
        page = self.currentWidget()
        return page.sizeHint() if page else super().sizeHint()

    def minimumSizeHint(self):
        page = self.currentWidget()
        return page.minimumSizeHint() if page else super().minimumSizeHint()

    def heightForWidth(self, width):
        page = self.currentWidget()
        if page is None:
            return super().heightForWidth(width)
        layout = page.layout()
        minimum = layout.minimumHeightForWidth(width) if layout else -1
        return max(page.minimumSizeHint().height(), minimum)


class MainWindow(QMainWindow):
    def __init__(self, data_dir: Path, demo=False):
        super().__init__()
        set_language(load_preference(data_dir))
        self.data_dir, self.demo = data_dir, demo
        self._closing = False
        self._database_closed = False
        self._history_opened = False
        self.repo = Repository(data_dir / "warehouse.db", self)
        self.controller = Controller(self.repo, demo, self)
        self.setWindowTitle(tr("利仓上位机") + (" [" + tr("模拟演示") + "]" if demo else ""))
        self.setMinimumSize(960, 640)
        self.resize(1280, 820)
        root = QWidget()
        root.setObjectName("canvas")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(6, 6, 6, 0)
        root_layout.setSpacing(6)
        workspace = QHBoxLayout()
        workspace.setSpacing(6)
        root_layout.addLayout(workspace, 1)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(180 if current_language() == 'zh_CN' else 240)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(8, 10, 8, 8)
        side.setSpacing(6)
        self.nav = []
        self.nav_routes = []
        self.nav_groups = []
        self.group_buttons = {}
        titles = [("粮情详情", "仓房粮情、温度测点与本地功能操作"),
                  ("测温分机管理", "管理本地串口、测温分机与探头登记"),
                  ("历史记录", "本地历史查询、曲线与报表导出"),
                  ("参数与报警", "微调、阈值和报警记录"),
                  ("本地维护", "备份、恢复、清理与通信日志"),
                  ("首页", "本地仓房、设备与粮情概况"),
                  ("仓房信息管理", "管理本地仓房及其测温设备"),
                  ("语言设置", "")]
        nav_area = QScrollArea()
        nav_area.setWidgetResizable(True)
        nav_area.setStyleSheet("QScrollArea,QWidget#navContent { background:transparent; border:none; }")
        nav_content = QWidget()
        nav_content.setObjectName("navContent")
        nav_layout = QVBoxLayout(nav_content)
        nav_layout.setContentsMargins(0, 4, 0, 0)
        nav_layout.setSpacing(4)

        def add_nav(label, page, target_layout, group=None, tab=None, mode=None):
            nav = QPushButton(tr(label))
            nav.setToolTip(tr(label))
            nav.setObjectName("nav")
            nav.setCheckable(True)
            nav.setProperty("child", group is not None)
            if group is None:
                nav.setIcon(navigation_icon("home"))
            nav.clicked.connect(lambda _, i=page, text=label, t=tab, m=mode: self.navigate(i, text, t, m))
            self.nav.append(nav)
            self.nav_routes.append((nav, page, label, group))
            target_layout.addWidget(nav)
            return nav

        add_nav("首页", 5, nav_layout, mode="home")
        groups = [
            ("仓房管理", "settings", [("仓房信息管理", 6, None), ("温度点管理", 0, 1)]),
            ("设备管理", "monitoring", [("测温分机管理", 1, None), ("串口设置", 1, None), ("探头登记", 1, None)]),
            ("采集规则", "grid", [(SETTINGS_TAB_TITLES[0], 3, 0)]),
            ("数据管理", "database", [("粮情总览", 5, None), ("粮情详情", 0, None), ("历史曲线", 2, None), ("历史记录", 2, None)]),
            ("系统设置", "settings", [(SETTINGS_TAB_TITLES[1], 3, 1), (SETTINGS_TAB_TITLES[2], 3, 2), ("本地维护", 4, None), ("语言设置", 7, None)]),
        ]
        for name, icon, children in groups:
            group_button = QPushButton(f"{tr(name)}   ▸")
            group_button.setCheckable(True)
            group_button.setIcon(navigation_icon(icon))
            group_button.setObjectName("navGroup")
            child_frame = QWidget()
            child_frame.setStyleSheet("background:transparent;")
            child_layout = QVBoxLayout(child_frame)
            child_layout.setContentsMargins(0, 0, 0, 0)
            child_layout.setSpacing(2)
            def toggle_group(expanded, frame=child_frame, button=group_button, label=name):
                if expanded:
                    for other in self.nav_groups:
                        if other is not button and other.isChecked():
                            other.setChecked(False)
                frame.setVisible(expanded)
                button.setText(f"{tr(label)}   {'▾' if expanded else '▸'}")

            group_button.toggled.connect(toggle_group)
            nav_layout.addWidget(group_button)
            nav_layout.addWidget(child_frame)
            self.nav_groups.append(group_button)
            self.group_buttons[child_frame] = group_button
            for label, page, tab in children:
                child = add_nav(label, page, child_layout, child_frame, tab, "overview" if label == "粮情总览" else None)
                if label == "报警记录":
                    self.alarm_nav = child
            child_frame.hide()
        nav_layout.addStretch()
        nav_area.setWidget(nav_content)
        side.addWidget(nav_area, 1)
        side.addStretch()
        side.addWidget(QLabel(tr("本地版 {version}", version=__version__)))
        workspace.addWidget(sidebar)
        content = QFrame()
        content.setObjectName("workArea")
        body = QVBoxLayout(content)
        body.setContentsMargins(14, 14, 14, 14)
        body.setSpacing(12)
        topbar = QFrame()
        topbar.setObjectName("topbar")
        top = QHBoxLayout(topbar)
        top.setContentsMargins(14, 12, 14, 12)
        top.setSpacing(12)
        site = QFrame()
        site.setObjectName("topbarInfo")
        site.setFixedWidth(154)
        site_layout = QVBoxLayout(site)
        site_layout.setContentsMargins(0, 0, 0, 0)
        site_layout.setSpacing(3)
        app_title = QLabel(tr("利仓上位机"))
        app_title.setObjectName("appTitle")
        site_layout.addWidget(app_title)
        self.local_summary = QLabel(tr("离线粮情监测"))
        self.local_summary.setObjectName("topbarSubtitle")
        self.local_summary.hide()
        top.addWidget(site)
        divider = QFrame()
        divider.setObjectName("topbarDivider")
        divider.setFixedSize(1, 24)
        top.addWidget(divider)
        labels = QVBoxLayout()
        labels.setSpacing(3)
        self.title = QLabel()
        self.title.setObjectName("moduleTitle")
        self.subtitle = QLabel()
        self.subtitle.setObjectName("topbarSubtitle")
        self.subtitle.setWordWrap(True)
        labels.addWidget(self.title)
        self.subtitle.hide()
        top.addLayout(labels, 1)
        self.connection_pill = QLabel(tr("串口未连接"))
        self.connection_pill.setObjectName("statusPill")
        self.alarm_pill = QLabel(tr("当前无温度预警"))
        self.alarm_pill.setObjectName("alarmPill")
        self.alarm_pill.hide()
        if demo:
            self.demo_badge = QLabel(tr("模拟演示"))
            self.demo_badge.setObjectName("demoBadge")
            self.demo_badge.setToolTip(tr("独立演示数据库，数值来自模拟器；不代表现场采集。"))
            top.addWidget(self.demo_badge)
        top.addWidget(self.connection_pill)
        top.addWidget(self.alarm_pill)
        self.connect_button = button(tr("连接全部"), self.controller.connect_all)
        top.addWidget(self.connect_button)
        self.connect_button.hide()
        self.run_button = button(tr("开始采集"), self.toggle_collection, True)
        top.addWidget(self.run_button)
        for widget in (self.connection_pill, self.alarm_pill, self.connect_button, self.run_button):
            widget.setFixedHeight(28)
            top.setAlignment(widget, Qt.AlignmentFlag.AlignVCenter)
        root_layout.insertWidget(0, topbar)
        self.clock = QLabel()
        self.clock.setObjectName("statusClock")
        self.statusBar().addPermanentWidget(self.clock)
        self.stack = ActivePageStack()
        self.live = LivePage(self.repo, self.controller)
        self.devices = DevicesPage(self.repo, self.controller)
        self.history = HistoryPage(self.repo, self.controller)
        self.settings = SettingsPage(self.repo, self.controller)
        self.maintenance = MaintenancePage(self.repo, self.controller, data_dir)
        self.overview = OverviewPage(self.repo, self.controller)
        self.warehouses = WarehousePage(self.repo, self.controller)
        self.language = LanguagePage(data_dir)
        self.overview.detail_requested.connect(self.open_detail)
        self.overview.history_requested.connect(lambda: self.navigate(2))
        self.overview.summary_requested.connect(self.open_summary)
        self.live.action_requested.connect(self.live_action)
        self.devices.mode_requested.connect(lambda mode: self.navigate(1, "探头登记"))
        self.maintenance.busy_changed.connect(self.set_maintenance_busy)
        for page in (self.live, self.devices, self.history, self.settings, self.maintenance, self.overview, self.warehouses, self.language):
            self.stack.addWidget(page)
        self.settings.tab_changed.connect(self._settings_tab_changed)
        page_scroll = QScrollArea()
        page_scroll.setWidgetResizable(True)
        page_scroll.setFrameShape(QFrame.Shape.NoFrame)
        page_scroll.setWidget(self.stack)
        body.addWidget(page_scroll, 1)
        workspace.addWidget(content, 1)
        self.page_titles = titles
        self.navigate(5)
        self.repo.error.connect(self.show_error)
        self.controller.log.connect(lambda message: self.statusBar().showMessage(tr(message), 6000))
        self.controller.running_changed.connect(lambda running: self.run_button.setText(tr("暂停采集") if running else tr("开始采集")))
        self.controller.alarms_changed.connect(self.update_alarms)
        self.controller.metadata_changed.connect(lambda _: self.repo.call(
            "get_setting", "point_layouts", {}, callback=self.live._layouts_loaded))
        self.controller.metadata_changed.connect(lambda _: self.settings.reload_settings())
        self.controller.metadata_changed.connect(lambda _: self.update_topbar())
        self.controller.connections_changed.connect(self.update_topbar)
        self.alarm_rows = []
        self.sound_timer = QTimer(self)
        self.sound_timer.setInterval(10000)
        self.sound_timer.timeout.connect(self.sound_alarm)
        self.sound_timer.start()
        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(1000)
        self.clock_timer.timeout.connect(lambda: self.clock.setText(QDateTime.currentDateTime().toString("yyyy-MM-dd  HH:mm:ss")))
        self.clock_timer.start()
        self.clock_timer.timeout.emit()
        self.statusBar().setToolTip(tr("本地数据：{path}", path=data_dir))
        self.repo.start()
        self.repo.thread.finished.connect(self._database_finished)

    def navigate(self, index, title=None, tab=None, mode=None):
        title = title or self.page_titles[index][0]
        if self.stack.currentIndex() == 1 and index != 1 and not self.devices.can_leave():
            return
        if index == 1:
            device_mode = {"探头登记": "registration", "串口设置": "ports", "串口连接": "ports"}.get(title, "devices")
            if not self.devices.set_mode(device_mode):
                return
        if index == 3:
            if tab is None and title in SETTINGS_TAB_TITLES:
                tab = SETTINGS_TAB_TITLES.index(title)
            if tab is not None:
                self.settings.set_tab(tab)
            title = SETTINGS_TAB_TITLES[self.settings.current_tab()]
        self.stack.setCurrentIndex(index)
        self._show_active_route(index, title)
        self.run_button.setObjectName("primary" if index == 5 else "globalAction")
        self.run_button.style().unpolish(self.run_button)
        self.run_button.style().polish(self.run_button)
        self.connect_button.setVisible(index == 1 and self.devices.mode == "ports")
        self.subtitle.setText({"测温分机管理": "管理仓房分机、板上地址与登记状态",
                               "串口设置": "选择实际 COM 口、设置通信参数并打开串口",
                               "探头登记": "扫描探头、核对测点并统一保存到分机"}.get(title, self.page_titles[index][1]))
        if index == 5:
            self.overview.set_mode(mode or ("overview" if title == "粮情总览" else "home"))
        if index == 0 and tab is not None:
            self.live.tabs.setCurrentIndex(tab)
        if index == 2:
            self.history.set_view_mode("curve" if title == "历史曲线" else "records")
            if not self._history_opened:
                self._history_opened = True
                self.history._recent()
            else:
                self.history.query()
        elif index == 4:
            # Restore can change saved point locations and alarm preferences.
            self.controller.refresh_alarms()

    def _settings_tab_changed(self, tab):
        if self.stack.currentWidget() is self.settings:
            self._show_active_route(3, SETTINGS_TAB_TITLES[tab])
            self.stack.updateGeometry()

    def _show_active_route(self, index, title):
        for nav, page, label, group in self.nav_routes:
            active = page == index and label == title
            nav.setChecked(active)
            if active and group:
                self.group_buttons[group].setChecked(True)
        self.title.setText(tr(title))

    def open_summary(self, key):
        routes = {'warehouses': (6, '仓房信息管理', None),
                  'devices': (1, '测温分机管理', None),
                  'points': (0, '温度点管理', 1),
                  'alarms': (3, '报警记录', 2)}
        if key in routes:
            self.navigate(*routes[key])

    def update_topbar(self):
        self.local_summary.setText(tr("{p0} 个仓房", p0=len(self.controller.metadata["warehouses"])))
        count = sum(self.controller.manager.is_connected(str(p['id'])) for p in self.controller.metadata['ports'])
        self.connection_pill.setText(tr("串口 {connected}/{total}", connected=count, total=len(self.controller.metadata["ports"])))
        self.connection_pill.setToolTip(tr("已打开 {count} 个串口。打开端口不代表分机已响应，设备管理中可查询在线状态。", count=count))

    def open_detail(self, identifier):
        self.navigate(0)
        self.live.warehouse.setCurrentIndex(0)
        self.live.device.setCurrentIndex(self.live.device.findData(identifier))

    def live_action(self, action):
        identifier = self.live.device.currentData()
        if action == "layout":
            self.live.edit_layout()
        elif action == "register":
            self.navigate(1, "探头登记")
            self.devices.registration_device.setCurrentIndex(self.devices.registration_device.findData(identifier))
            self.devices.register()
        elif action == "calibration":
            self.navigate(3, "温湿度微调", 1)
            self.settings.device_combo.setCurrentIndex(self.settings.device_combo.findData(identifier))
        elif action in {"history", "export"}:
            self.navigate(2)
            self.history.device_combo.setCurrentIndex(self.history.device_combo.findData(identifier))
            self.history.query()
            if action == "export":
                self.history.export("xlsx")

    def toggle_collection(self):
        if not self.controller.running:
            if not self.controller.metadata["devices"]:
                QMessageBox.information(self, tr("尚未配置分机"), tr("请先在“测温分机管理”中添加仓房、串口和分机。"))
                self.navigate(1)
                return
            self.controller.connect_all()
        self.controller.set_running(not self.controller.running)

    def set_maintenance_busy(self, busy):
        self.connect_button.setEnabled(not busy)
        self.run_button.setEnabled(not busy)
        for nav in self.nav + self.nav_groups:
            nav.setEnabled(not busy)
        for page in (self.live, self.devices, self.history, self.settings, self.overview, self.warehouses):
            page.setEnabled(not busy)

    def show_error(self, message):
        self.controller.log.emit(message)
        QMessageBox.warning(self, tr("操作未完成"), tr(message))

    def update_alarms(self, rows):
        self.alarm_rows = rows
        count = sum(bool(r.get("active")) and not r.get("acknowledged_at") for r in rows)
        self.alarm_nav.setText(tr("报警记录") + (f" · {count}" if count else ""))
        self.alarm_pill.setText(tr("待处理报警") + f" {count}")
        self.alarm_pill.setVisible(bool(count))

    def sound_alarm(self):
        if self.settings.sound_enabled and any(r.get("active") and not r.get("acknowledged_at") for r in self.alarm_rows):
            QApplication.beep()

    def closeEvent(self, event: QCloseEvent):
        if self._database_closed:
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        if not self.devices.can_leave():
            return
        self._closing = True
        self.sound_timer.stop()
        self.controller.shutdown()
        self.setEnabled(False)
        self.statusBar().showMessage(tr("正在完成本地任务并关闭，请稍候…"))
        self.repo.begin_shutdown()

    def _database_finished(self):
        self._database_closed = True
        if self._closing:
            self.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="粮情监测 Windows/macOS 离线上位机")
    parser.add_argument("--demo", action="store_true", help="使用隔离的模拟设备与演示数据库")
    parser.add_argument("--data-dir", type=Path, help="自定义数据目录（演示模式会附加 demo 子目录）")
    parser.add_argument("--capture", type=Path, help="开发验收：保存主窗口截图")
    parser.add_argument("--quit-after", type=float, default=0, help="开发验收：运行指定秒数后退出")
    args = parser.parse_args(argv)
    app = QApplication([sys.argv[0]])
    app.setApplicationName("WarehouseTemperatureConsole")
    app.setOrganizationName("WarehouseTemperatureSystem")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    data_dir = args.data_dir or Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))
    if args.demo:
        data_dir = data_dir / "demo"
    data_dir.mkdir(parents=True, exist_ok=True)
    saved_language = load_preference(data_dir, default=None)
    # The packaged build smoke test launches an unattended timed screenshot.
    unattended_capture = args.capture is not None and args.quit_after > 0
    set_language(saved_language or ("zh_CN" if unattended_capture else "system"))
    lock = QLockFile(str(data_dir / "application.lock"))
    if not lock.tryLock(100):
        QMessageBox.information(None, tr("程序已打开"), tr("该数据目录已有一个程序实例运行，请切换到已打开的窗口。"))
        return 1
    try:
        if saved_language is None and not unattended_capture:
            if FirstLaunchLanguageDialog(data_dir).exec() != QDialog.DialogCode.Accepted:
                return 0
        handler = RotatingFileHandler(data_dir / "application.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        logging.basicConfig(level=logging.INFO, handlers=[handler],
                            format="%(asctime)s %(levelname)s %(message)s", force=True)
        window = MainWindow(data_dir, args.demo)
        window.show()
        if args.capture:
            args.capture.parent.mkdir(parents=True, exist_ok=True)
            QTimer.singleShot(3500, lambda: window.grab().save(str(args.capture)))
        if args.quit_after > 0:
            QTimer.singleShot(int(args.quit_after * 1000), window.close)
        return app.exec()
    finally:
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())

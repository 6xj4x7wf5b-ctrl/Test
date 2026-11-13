"""Standalone launcher; numbering records are isolated from collection data."""
from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys

from PySide6.QtCore import QLockFile, QStandardPaths, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from warehouse_pc.style import STYLE
from .service import NumberingService
from .ui import NUMBERING_STYLE, NumberingWindow


def main(argv=None):
    parser = argparse.ArgumentParser(description='GXHT3W 探头编号工具')
    parser.add_argument('--data-dir', type=Path, help='独立编号记录目录')
    parser.add_argument('--demo', action='store_true', help='使用隔离的模拟编号工装')
    parser.add_argument('--capture', type=Path, help='开发验收：保存窗口截图')
    parser.add_argument('--quit-after', type=float, default=0, help='开发验收：运行指定秒数后关闭')
    args = parser.parse_args(argv)
    app = QApplication([sys.argv[0]])
    app.setApplicationName('GXHTProbeNumbering')
    app.setOrganizationName('WarehouseTemperatureSystem')
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE + NUMBERING_STYLE)
    data_dir = args.data_dir or Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))
    if args.demo:
        data_dir /= 'demo'
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(data_dir / 'application.lock'))
    if not lock.tryLock(100):
        QMessageBox.information(None, '程序已打开', '该编号记录目录已有程序运行，请切换到已打开的窗口。')
        return 1
    service = None
    try:
        handler = RotatingFileHandler(data_dir / 'numbering.log', maxBytes=1_000_000, backupCount=3, encoding='utf8')
        logging.basicConfig(level=logging.INFO, handlers=[handler],
                            format='%(asctime)s %(levelname)s %(message)s', force=True)
        service = NumberingService(data_dir, demo=args.demo)
        window = NumberingWindow(service, data_dir, demo=args.demo)
        window.show()
        if args.demo:
            # Read-only demonstration at launch; no numbering is written automatically.
            loaded = False

            def demo_connected(state):
                nonlocal loaded
                if state.fixture and not state.busy and not loaded:
                    loaded = True
                    QTimer.singleShot(0, service.read_probe)

            service.state_changed.connect(demo_connected)
            QTimer.singleShot(150, lambda: service.connect_fixture('模拟工装'))
        if args.capture:
            args.capture.parent.mkdir(parents=True, exist_ok=True)
            delay = min(2500, max(100, int(args.quit_after * 1000) - 250)) if args.quit_after > 0 else 2500
            QTimer.singleShot(delay, lambda: window.grab().save(str(args.capture)))
        if args.quit_after > 0:
            QTimer.singleShot(int(args.quit_after * 1000), window.close)
        return app.exec()
    finally:
        if service is not None:
            service.close()
        lock.unlock()


if __name__ == '__main__':
    raise SystemExit(main())

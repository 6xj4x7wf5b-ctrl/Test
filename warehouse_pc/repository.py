"""Run all SQLite work on one dedicated thread; callbacks run on the GUI thread."""
from __future__ import annotations

import itertools
import logging
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .storage import Store


class _Worker(QObject):
    completed = Signal(int, object, str)
    initialized = Signal(str)

    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.store = None

    @Slot()
    def initialize(self):
        try:
            self.store = Store(self.path)
            self.initialized.emit("")
        except Exception as exc:
            self.initialized.emit(str(exc))

    @Slot(int, str, object, object)
    def execute(self, token, method, args, kwargs):
        try:
            if self.store is None:
                raise RuntimeError("本地数据库尚未就绪")
            if method == "metadata":
                result = {key: getattr(self.store, key)() for key in ("warehouses", "ports", "devices")}
            elif method == "demo_seed":
                if not self.store.devices():
                    warehouse = self.store.save_warehouse("演示粮仓")
                    for i, protocol in enumerate(("LC-M3", "Modbus RTU"), 1):
                        port = self.store.save_port(f"模拟通道 {i}", f"simulator://{i}", 9600)
                        device = self.store.save_device(f"模拟分机 {i}", warehouse, port, 1,
                                                        protocol, 20 if i == 1 else 165, 1)
                        self.store.save_threshold(device, "grain", temp_high=30)
                        self.store.save_threshold(device, "ambient", temp_high=35, humidity_high=75)
                result = None
            else:
                result = getattr(self.store, method)(*args, **kwargs)
            self.completed.emit(token, result, "")
        except Exception as exc:
            logging.exception("Database operation %s failed", method)
            self.completed.emit(token, None, str(exc))

    @Slot()
    def close(self):
        if self.store is not None:
            self.store.close()
        QThread.currentThread().quit()


class Repository(QObject):
    ready = Signal()
    error = Signal(str)
    requested = Signal(int, str, object, object)
    closing = Signal()

    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.thread = QThread(self)
        self.worker = _Worker(path)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.initialize)
        self.worker.initialized.connect(self._initialized)
        self.requested.connect(self.worker.execute)
        self.worker.completed.connect(self._completed)
        self.closing.connect(self.worker.close)
        self.thread.finished.connect(self.worker.deleteLater)
        self.callbacks = {}
        self.tokens = itertools.count(1)
        self._closing = False

    def start(self):
        self.thread.start()

    def call(self, method, *args, callback=None, on_error=None, **kwargs):
        if self._closing:
            return None
        token = next(self.tokens)
        self.callbacks[token] = (callback, on_error)
        self.requested.emit(token, method, args, kwargs)
        return token

    @Slot(str)
    def _initialized(self, error):
        if error:
            self.error.emit(error)
        else:
            self.ready.emit()

    @Slot(int, object, str)
    def _completed(self, token, result, error):
        callback, on_error = self.callbacks.pop(token, (None, None))
        if error:
            if on_error:
                on_error(error)
            else:
                self.error.emit(error)
            return
        if callback:
            try:
                callback(result)
            except Exception:
                logging.exception("UI callback failed")
                self.error.emit("界面处理失败，详情已写入本地日志")

    def begin_shutdown(self):
        if not self._closing:
            self._closing = True
            self.closing.emit()

    def shutdown(self):
        self.begin_shutdown()
        return self.thread.wait(10000)

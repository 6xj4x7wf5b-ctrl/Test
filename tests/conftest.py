import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session", autouse=True)
def qt_application():
    # Transport tests use QCoreApplication.instance(); create the widget-capable
    # subclass first so all suites can share one Qt event loop safely.
    app = QApplication.instance() or QApplication([])
    yield app

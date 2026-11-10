"""Dashboard counts link to the matching pages, including when they are zero."""

import time

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from warehouse_pc.app import MainWindow


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert predicate()


def test_summary_cards_navigate_by_mouse_and_keyboard(qt_application, tmp_path):
    window = MainWindow(tmp_path / "site", demo=False)
    window.show()
    window.activateWindow()
    overview = window.overview
    requested = []
    overview.summary_requested.connect(requested.append)

    try:
        wait_for(lambda: overview.metric_values[3].text() == "0")
        assert [value.text() for value in overview.metric_values[:4]] == ["0"] * 4
        assert len(overview.metric_cards) == 4

        first_card = overview.metric_cards[0]
        QTest.mouseClick(overview.metric_values[0], Qt.MouseButton.RightButton)
        QTest.mousePress(first_card, Qt.MouseButton.LeftButton)
        QTest.mouseRelease(first_card, Qt.MouseButton.LeftButton, pos=QPoint(-3, -3))
        assert requested == []

        routes = (
            ("warehouses", window.warehouses, None),
            ("devices", window.devices, Qt.Key.Key_Return),
            ("points", window.live, Qt.Key.Key_Space),
            ("alarms", window.settings, None),
        )
        for index, (key, page, keyboard) in enumerate(routes):
            window.navigate(5, "首页")
            qt_application.processEvents()
            card = overview.metric_cards[index]
            if keyboard is None:
                QTest.mouseClick(overview.metric_values[index], Qt.MouseButton.LeftButton)
            else:
                card.setFocus(Qt.FocusReason.TabFocusReason)
                assert card.hasFocus()
                QTest.keyClick(card, keyboard)
            assert requested == [route[0] for route in routes[:index + 1]]
            assert window.stack.currentWidget() is page
            if key == "devices":
                assert window.devices.mode == "devices"
            if key == "points":
                tabs = window.live.tabs
                assert tabs.tabText(tabs.currentIndex()) == "测点列表"
            if key == "alarms":
                assert window.settings.current_tab() == 2
    finally:
        window.close()
        wait_for(lambda: not window.repo.thread.isRunning())

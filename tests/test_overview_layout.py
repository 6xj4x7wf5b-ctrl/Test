"""Warehouse cards must reflow without clipping when data or window size changes."""
from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest

from warehouse_pc.overview_ui import OverviewPage
from warehouse_pc.style import STYLE


class DashboardController(QObject):
    metadata_changed = Signal(object)
    samples_changed = Signal()
    connections_changed = Signal()
    alarms_changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.metadata = {"warehouses": [{"id": 1, "name": "一号仓房"}], "devices": []}
        self.running = False
        self.latest = {}

    def refresh_alarms(self):
        pass

    def poll(self):
        pass


def test_cards_fit_after_loading_multiple_rows_resizing_and_turning_pages(qt_application):
    controller = DashboardController()
    page = OverviewPage(None, controller)
    page.setStyleSheet(STYLE)
    page.timer.stop()
    page.resize(1040, 700)
    page.show()

    def settle():
        for _ in range(4):
            qt_application.processEvents()
            QTest.qWait(5)

    def assert_cards_fit(count):
        settle()
        assert page.scroll.horizontalScrollBar().maximum() == 0
        cards = [card for card, _, _ in page._cards.values()]
        assert len(cards) == count
        bounds = page.card_area.rect()
        for index, card in enumerate(cards):
            assert bounds.contains(card.geometry())
            assert all(not card.geometry().intersects(other.geometry()) for other in cards[index + 1:])

    try:
        assert_cards_fit(1)
        assert not page.pager.isVisible()
        controller.metadata["warehouses"] += [
            {"id": index, "name": "测试长名称的仓房仍然完整显示" if index == 2 else f"{index} 号仓房"}
            for index in range(2, 15)
        ]
        page.refresh()
        assert_cards_fit(12)
        assert page.pager.isVisible()
        for width in (740, 1040):
            page.resize(width, 600)
            assert_cards_fit(12)
            page.next_button.click()
            assert_cards_fit(2)
            page.view_combo.setCurrentIndex(1)
            settle()
            assert page.table.rowCount() == 2
            page.view_combo.setCurrentIndex(0)
            assert_cards_fit(2)
            page.prev_button.click()
            assert_cards_fit(12)
        page.set_mode("overview")
        page.keyword.setText("没有这个仓房")
        page.apply_filters()
        settle()
        assert page.empty_label.isVisible()
        assert not page.views.isVisible()
        assert not page.pager.isVisible()
    finally:
        page.close()
        page.deleteLater()

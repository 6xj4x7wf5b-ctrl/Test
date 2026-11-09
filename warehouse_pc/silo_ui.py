"""Native Qt, display-only cylindrical grain-temperature schematic."""
from __future__ import annotations

import html
import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QToolTip, QVBoxLayout, QWidget,
)

from .silo_model import SiloLayer, SiloPoint, SiloSnapshot

_STATUS = {"ok": "正常", "offline": "离线", "stale": "数据过期", "not_ready": "尚未采样",
           "unbound": "未绑定", "crc_error": "校验错误", "bus_error": "总线故障",
           "power_error": "供电错误", "timeout": "转换超时", "data_error": "数据无效"}


def _valid(point: SiloPoint | None) -> bool:
    return bool(point is not None and point.status == "ok" and point.temperature is not None
                and math.isfinite(point.temperature))


def _temperature(point: SiloPoint | None) -> str:
    return f"{point.temperature:.1f}" if _valid(point) else "—"


def _ring_counts(total: int) -> tuple[int, int, int]:
    if total < 9:
        return 0, 0, total
    inner = max(1, round(total * .22))
    middle = max(inner + 1, round(total * .33))
    outer = total - inner - middle
    while outer <= middle and inner > 1:
        inner -= 1
        outer += 1
    return (inner, middle, outer) if outer > middle else (0, 0, total)


class _SiloCanvas(QWidget):
    point_selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(430, 220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.snapshot = SiloSnapshot(None, (), "", "", 0)
        self.layer: SiloLayer | None = None
        self.display_mode = "temperature"
        self.selected_point: int | None = None
        self.hit_regions: list[tuple[QRectF, SiloPoint | None, str]] = []

    def sizeHint(self):
        return QSize(780, 230)

    def _geometry(self):
        width, height = self.width(), self.height()
        cx = width / 2
        rx = max(140, min(400, (width - 100) / 2))
        cap = min(56, height * .18)
        top, bottom = max(height * .22, cap + 28), height * .78
        cy = height / 2
        if self.layer and self.layer.registered and self.layer.points:
            names = list(dict.fromkeys(layer.points[0].layer for layer in self.snapshot.layers
                                       if layer.registered and layer.points))
            name = self.layer.points[0].layer
            if len(names) > 1 and name in names:
                cy += (names.index(name) / (len(names) - 1) - .5) * (bottom - top) * .32
        return cx, rx, top, bottom, cap, cy

    def _slot_rectangles(self, cx, rx, cy):
        slots = self.layer.slots if self.layer else ()
        counts = _ring_counts(len(slots))
        outer_x = rx - 34
        outer_y = min(78, max(58, self.height() * .28))
        occupied: list[QRectF] = []
        result = []
        slot_index = 0
        for ring_index, (count, scale) in enumerate(zip(counts, (.31, .61, 1.0))):
            if not count:
                continue
            ring_x, ring_y = outer_x * scale, outer_y * scale
            phase = -math.pi / 2 + (math.pi / count if ring_index == 1 else 0)
            for number in range(count):
                angle = phase + number * 2 * math.pi / count
                anchor = QPointF(cx + math.cos(angle) * ring_x, cy + math.sin(angle) * ring_y)
                rect = QRectF(anchor.x() - 25, anchor.y() - 10, 50, 20)
                # Keep labels readable in the compact view. Offsets depend only
                # on slots, so a missing reading never moves another cable.
                for offset in (0, -4, 4, -8, 8, -12, 12, -16, 16, -20, 20):
                    candidate = rect.translated(0, offset)
                    if candidate.top() < 24 or candidate.bottom() > self.height() - 10:
                        continue
                    if not any(candidate.adjusted(-2, -2, 2, 2).intersects(previous) for previous in occupied):
                        rect = candidate
                        break
                occupied.append(rect)
                result.append((slots[slot_index], rect, anchor))
                slot_index += 1
        return result

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#c5cfda"), 1))
        painter.setBrush(QColor("#ffffff"))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 2, 2)
        font = QFont(self.font())
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(QColor("#66778a"))
        painter.drawText(QRectF(12, 5, self.width() - 24, 18), Qt.AlignmentFlag.AlignLeft,
                         "圆柱分层测温示意 · 环形位置不代表实际线距和埋深")
        cx, rx, top, bottom, cap, cy = self._geometry()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#f7f9fb"))
        painter.drawRect(QRectF(cx - rx, top, 2 * rx, bottom - top))
        painter.setPen(QPen(QColor("#bcc9d5"), 1))
        painter.setBrush(QColor("#eef3f7"))
        painter.drawEllipse(QRectF(cx - rx, top - cap, rx * 2, cap * 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPointF(cx - rx, top), QPointF(cx - rx, bottom))
        painter.drawLine(QPointF(cx + rx, top), QPointF(cx + rx, bottom))
        painter.setPen(QPen(QColor("#c2cdd7"), 1, Qt.PenStyle.DashLine))
        painter.drawEllipse(QRectF(cx - rx, bottom - cap, rx * 2, cap * 2))
        painter.setPen(QPen(QColor("#aabac9"), 1))
        painter.drawArc(QRectF(cx - rx, bottom - cap, rx * 2, cap * 2), 180 * 16, 180 * 16)
        self.hit_regions = []
        if not self.layer or not self.layer.slots:
            painter.setPen(QColor("#66778a"))
            painter.drawText(QRectF(cx - rx, cy - 12, rx * 2, 24), Qt.AlignmentFlag.AlignCenter, "暂无粮温测点")
            return
        outer_x = rx - 34
        outer_y = min(78, max(58, self.height() * .28))
        painter.setPen(QPen(QColor("#b0c5da"), 1))
        painter.setBrush(QColor(232, 240, 248, 115))
        painter.drawEllipse(QRectF(cx - outer_x, cy - outer_y, 2 * outer_x, 2 * outer_y))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#b5c7d8"), 1, Qt.PenStyle.DashLine))
        for scale in (.31, .61):
            painter.drawEllipse(QRectF(cx - outer_x * scale, cy - outer_y * scale,
                                      outer_x * scale * 2, outer_y * scale * 2))
        by_slot = {point.slot: point for point in self.layer.points}
        positions = self._slot_rectangles(cx, rx, cy)
        metrics = QFontMetrics(font)
        for slot, rect, anchor in positions:
            point = by_slot.get(slot)
            valid = _valid(point)
            alarm = valid and point.alarm
            if point is None:
                background, border, foreground = "#ffffff", "#aebbc8", "#8693a0"
                label = slot if self.display_mode == "cable" else "空位"
            else:
                background, border, foreground = (("#fdecec", "#bd5353", "#a03e3e") if alarm else
                    ("#edf7ef", "#58946c", "#2d6845") if valid else ("#f0f2f4", "#a9b4bf", "#75818e"))
                label = (_temperature(point) if self.display_mode == "temperature" else
                         f"#{point.point:03}" if self.display_mode == "point" else point.cable or "未登记")
            if abs(rect.center().y() - anchor.y()) > 2:
                painter.setPen(QPen(QColor("#b4c2cf"), 1))
                painter.drawLine(anchor, rect.center())
            if point is not None and point.point == self.selected_point:
                painter.setPen(QPen(QColor("#2f6fdf"), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(rect.adjusted(-3, -3, 3, 3))
            painter.setPen(QPen(QColor(border), 1, Qt.PenStyle.DashLine if point is None else Qt.PenStyle.SolidLine))
            painter.setBrush(QColor(background))
            painter.drawEllipse(rect)
            painter.setPen(QColor(foreground))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             metrics.elidedText(str(label), Qt.TextElideMode.ElideRight, int(rect.width()) - 6))
            self.hit_regions.append((rect, point, slot))
        painter.setPen(QColor("#65778a"))
        label = self.layer.label if self.layer.registered else "未布点 · 中央分组示意"
        painter.drawText(QRectF(self.width() - 162, self.height() - 20, 150, 15),
                         Qt.AlignmentFlag.AlignRight, label)

    def _hit(self, position):
        return next((item for item in reversed(self.hit_regions) if item[0].contains(position)), None)

    def mouseMoveEvent(self, event):
        item = self._hit(event.position())
        if item is None:
            QToolTip.hideText()
            self.unsetCursor()
            return
        _, point, slot = item
        self.setCursor(Qt.CursorShape.PointingHandCursor if point is not None else Qt.CursorShape.ArrowCursor)
        if point is None:
            text = f"线位：{html.escape(slot)}<br>本层没有对应测点，空心标记仅保留线位。"
        else:
            temperature = _temperature(point) + (" ℃" if _valid(point) else "")
            state = "温度越限" if _valid(point) and point.alarm else _STATUS.get(point.status, "无有效数据")
            text = (f"<b>粮温测点 #{point.point:03}</b><br>温度：{temperature}<br>状态：{state}<br>"
                    f"测温线：{html.escape(point.cable or '未登记')}　层：{html.escape(point.layer or '未登记')}<br>"
                    f"位置：{html.escape(point.position or '未登记')}")
        QToolTip.showText(event.globalPosition().toPoint(), text, self)

    def leaveEvent(self, event):
        QToolTip.hideText()
        self.unsetCursor()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            item = self._hit(event.position())
            if item is not None and item[1] is not None:
                self.selected_point = item[1].point
                self.update()
                self.point_selected.emit(item[1].point)
        super().mousePressEvent(event)


class SiloView(QWidget):
    point_selected = Signal(int)
    edit_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.snapshot = SiloSnapshot(None, (), "请先选择仓房与分机。", "", 0)
        self.selected_point: int | None = None
        self._expanded_view = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 5, 6, 5)
        layout.setSpacing(3)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        toolbar.addWidget(QLabel("层 / 分组"))
        self.layer_combo = QComboBox()
        self.layer_combo.setMinimumWidth(155)
        self.layer_combo.setMaximumWidth(270)
        self.layer_combo.currentIndexChanged.connect(self._layer_changed)
        toolbar.addWidget(self.layer_combo, 1)
        self.display_combo = QComboBox()
        for label, mode in (("温度（℃）", "temperature"), ("测点编号", "point"), ("测温线号", "cable")):
            self.display_combo.addItem(label, mode)
        self.display_combo.currentIndexChanged.connect(self._display_changed)
        toolbar.addWidget(self.display_combo)
        self.edit_button = QPushButton("调整布点")
        self.edit_button.clicked.connect(self.edit_requested.emit)
        toolbar.addWidget(self.edit_button)
        self.expand_button = QPushButton("放大")
        self.expand_button.clicked.connect(self._expand)
        toolbar.addWidget(self.expand_button)
        layout.addLayout(toolbar)
        self.canvas = _SiloCanvas(self)
        self.canvas.point_selected.connect(self._point_chosen)
        layout.addWidget(self.canvas, 1)
        footer = QHBoxLayout()
        footer.setSpacing(10)
        legend = QLabel('<span style="color:#58946c">●</span> 正常　'
                        '<span style="color:#bd5353">●</span> 越限　'
                        '<span style="color:#9ca9b5">●</span> 无有效值')
        legend.setStyleSheet("color:#66778a;font-size:10px;")
        footer.addWidget(legend)
        self.selection_label = QLabel("点击测点查看编号与状态")
        self.selection_label.setMinimumWidth(0)
        self.selection_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.selection_label.setStyleSheet("color:#2d527b;font-size:11px;")
        footer.addWidget(self.selection_label, 1)
        layout.addLayout(footer)
        self.message_label = QLabel(self.snapshot.message)
        self.message_label.setWordWrap(True)
        self.message_label.setMaximumHeight(30)
        self.message_label.setStyleSheet("color:#66778a;font-size:11px;")
        layout.addWidget(self.message_label)

    def sizeHint(self):
        return QSize(780, 300)

    def set_snapshot(self, snapshot: SiloSnapshot):
        old_key = self.layer_combo.currentData()
        changed_device = snapshot.device_id != self.snapshot.device_id
        if changed_device:
            self.selected_point = None
            old_key = None
        self.snapshot = snapshot
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        for layer in snapshot.layers:
            self.layer_combo.addItem(layer.label, layer.key)
        index = self.layer_combo.findData(old_key)
        self.layer_combo.setCurrentIndex(index if index >= 0 else (0 if snapshot.layers else -1))
        self.layer_combo.blockSignals(False)
        self.layer_combo.setEnabled(bool(snapshot.layers))
        self.display_combo.setEnabled(bool(snapshot.layers))
        self.edit_button.setEnabled(snapshot.device_id is not None and snapshot.grain_count > 0)
        self.canvas.snapshot = snapshot
        self.canvas.selected_point = self.selected_point
        source = "模拟数据" if snapshot.source == "simulator" else "设备数据" if snapshot.source else ""
        self.message_label.setText(f"{source} · {snapshot.message}" if source else snapshot.message)
        self.message_label.setToolTip(snapshot.message)
        self._layer_changed()
        if self._expanded_view is not None:
            self._expanded_view.set_snapshot(snapshot)

    def _layer_changed(self, *_):
        key = self.layer_combo.currentData()
        self.canvas.layer = next((layer for layer in self.snapshot.layers if layer.key == key), None)
        self.canvas.update()
        self._selection_text()

    def _display_changed(self, *_):
        self.canvas.display_mode = self.display_combo.currentData()
        self.canvas.update()

    def _point_chosen(self, point):
        self.selected_point = point
        self.canvas.selected_point = point
        self.canvas.update()
        self._selection_text()
        self.point_selected.emit(point)

    def _selection_text(self):
        point = next((point for layer in self.snapshot.layers for point in layer.points
                      if point.point == self.selected_point), None)
        if point is None:
            self.selection_label.setText("点击测点查看编号与状态")
            self.selection_label.setToolTip("")
            return
        status = "温度越限" if _valid(point) and point.alarm else _STATUS.get(point.status, "无有效数据")
        text = f"测点 #{point.point:03} · {_temperature(point)}{' ℃' if _valid(point) else ''} · {status}"
        self.selection_label.setText(text)
        self.selection_label.setToolTip(f"{text}\n测温线：{point.cable or '未登记'}　层：{point.layer or '未登记'}\n位置：{point.position or '未登记'}")

    def _expand(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("粮仓分层测温示意 · 放大")
        dialog.resize(1060, 560)
        layout = QVBoxLayout(dialog)
        view = SiloView(dialog)
        view.setMaximumHeight(16_777_215)
        view.canvas.setMinimumHeight(350)
        view.expand_button.hide()
        view.set_snapshot(self.snapshot)
        view.layer_combo.setCurrentIndex(max(0, view.layer_combo.findData(self.layer_combo.currentData())))
        view.display_combo.setCurrentIndex(self.display_combo.currentIndex())
        if self.selected_point is not None:
            view._point_chosen(self.selected_point)
        view.point_selected.connect(self._point_chosen)
        view.edit_requested.connect(self.edit_requested.emit)
        layout.addWidget(view)
        close_button = QPushButton("关闭放大视图")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button)
        self._expanded_view = view
        dialog.exec()
        self._expanded_view = None
        key = view.layer_combo.currentData()
        index = self.layer_combo.findData(key)
        if index >= 0:
            self.layer_combo.setCurrentIndex(index)
        self.display_combo.setCurrentIndex(view.display_combo.currentIndex())
        dialog.deleteLater()

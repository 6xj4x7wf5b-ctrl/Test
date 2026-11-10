"""Shared full-width filter panel with labels above fields and actions on the right."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout


class FilterPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("filterPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._body = QVBoxLayout(self)
        self._body.setContentsMargins(12, 10, 12, 12)
        self._body.setSpacing(10)
        self._rows = {}

    def _row(self, index):
        if index not in self._rows:
            fields = QHBoxLayout()
            fields.setSpacing(10)
            actions = QHBoxLayout()
            actions.setSpacing(8)
            fields.addLayout(actions)
            position = sum(key < index for key in self._rows)
            self._body.insertLayout(position, fields)
            self._rows[index] = fields, actions
        return self._rows[index]

    def add_field(self, text, widget, *, row=0, stretch=1):
        fields, _ = self._row(row)
        column = QVBoxLayout()
        column.setSpacing(3)
        label = QLabel(text)
        label.setObjectName("filterLabel")
        label.setBuddy(widget)
        if not widget.accessibleName():
            widget.setAccessibleName(text)
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        column.addWidget(label)
        column.addWidget(widget)
        fields.insertLayout(fields.count() - 1, column, stretch)
        return label

    def add_action(self, widget, *, row=0):
        _, actions = self._row(row)
        actions.addWidget(widget, 0, Qt.AlignmentFlag.AlignBottom)

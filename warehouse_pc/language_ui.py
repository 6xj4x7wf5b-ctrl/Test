"""Choose the interface language during setup or for the next launch."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout, QWidget,
)
from .filter_panel import FilterPanel
from .i18n import LANGUAGES, current_language, load_preference, save_preference, tr


def _language_combo(selected):
    combo = QComboBox()
    for code, name in LANGUAGES.items():
        combo.addItem(tr('跟随系统') if code == 'system' else name, code)
    combo.setCurrentIndex(combo.findData(selected))
    return combo


class FirstLaunchLanguageDialog(QDialog):
    def __init__(self, data_dir):
        super().__init__()
        self.data_dir = data_dir
        self.setWindowTitle(tr('语言设置'))
        self.setMinimumWidth(340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        self.language_combo = _language_combo('system')
        prompt = QLabel(tr('请选择界面语言'))
        prompt.setObjectName('sectionTitle')
        prompt.setBuddy(self.language_combo)
        layout.addWidget(prompt)
        layout.addWidget(self.language_combo)

        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setObjectName('banner')
        self.status.hide()
        layout.addWidget(self.status)

        buttons = QDialogButtonBox()
        self.continue_button = buttons.addButton(tr('继续'), QDialogButtonBox.ButtonRole.AcceptRole)
        self.continue_button.setObjectName('primary')
        self.continue_button.setDefault(True)
        buttons.addButton(tr('退出'), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._save_and_continue)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.language_combo.setFocus()

    def _save_and_continue(self):
        try:
            save_preference(self.data_dir, self.language_combo.currentData())
        except OSError as error:
            self.status.setText(tr('语言设置保存失败：{error}', error=str(error)))
            self.status.show()
            return
        self.accept()


class LanguagePage(QWidget):
    def __init__(self, data_dir, parent=None):
        super().__init__(parent)
        self.data_dir = data_dir
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        panel = FilterPanel()
        self.language_combo = _language_combo(load_preference(data_dir))
        panel.add_field(tr('界面语言'), self.language_combo)
        self.save_button = QPushButton(tr('保存设置'))
        self.save_button.setObjectName('primary')
        self.save_button.clicked.connect(self.save)
        panel.add_action(self.save_button)
        layout.addWidget(panel)
        current = QLabel(tr('当前语言：{language}', language=LANGUAGES[current_language()]))
        current.setObjectName('sectionMeta')
        layout.addWidget(current)
        note = QLabel(tr('更改后重新打开软件生效。跟随系统支持中文、英语、德语、法语、俄语，其他语言使用中文。'))
        note.setObjectName('muted')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()

    def save(self):
        try:
            save_preference(self.data_dir, self.language_combo.currentData())
        except OSError as error:
            self.status.setText(tr('语言设置保存失败：{error}', error=str(error)))
            return
        self.status.setText(tr('语言设置已保存，重新打开软件后生效。'))

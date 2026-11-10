"""Language choices persist without changing identifiers or formatting fields."""
import ast
from pathlib import Path
from string import Formatter
from unittest.mock import Mock

import pytest

from warehouse_pc import app as console, language_ui
from warehouse_pc.i18n import (LANGUAGES, catalogs, current_language, load_preference,
                               resolve_language, save_preference, set_language, tr)
from warehouse_pc.language_ui import FirstLaunchLanguageDialog, LanguagePage


def test_language_selection_persists_and_requires_restart(qt_application, tmp_path):
    set_language('zh_CN')
    assert load_preference(tmp_path) == 'zh_CN'
    assert load_preference(tmp_path, default=None) is None
    page = LanguagePage(tmp_path)
    assert page.language_combo.count() == 6
    page.language_combo.setCurrentIndex(page.language_combo.findData('de'))
    page.save_button.click()
    assert load_preference(tmp_path) == 'de'
    assert current_language() == 'zh_CN'
    assert '重新打开' in page.status.text()
    for language in LANGUAGES:
        save_preference(tmp_path, language)
        assert load_preference(tmp_path) == language
    (tmp_path / 'ui_preferences.json').write_text('{broken', encoding='utf8')
    assert load_preference(tmp_path) == 'zh_CN'
    assert load_preference(tmp_path, default=None) is None
    page.deleteLater()


def test_first_launch_precedes_main_window_and_keeps_demo_preference_separate(tmp_path, monkeypatch):
    decisions = iter((None, 'de', 'fr'))
    prompts = []
    windows = []

    class Dialog(FirstLaunchLanguageDialog):
        def exec(self):
            prompts.append(self.data_dir)
            assert not (self.data_dir / 'warehouse.db').exists()
            decision = next(decisions)
            if decision is None:
                self.close()
            else:
                self.language_combo.setCurrentIndex(self.language_combo.findData(decision))
                self.continue_button.click()
            return self.result()

    def window(data_dir, demo):
        windows.append((demo, load_preference(data_dir, default=None)))
        return Mock()

    fake_app = Mock()
    fake_app.exec.return_value = 0
    monkeypatch.setattr(console, 'QApplication', lambda _: fake_app)
    monkeypatch.setattr(console, 'FirstLaunchLanguageDialog', Dialog)
    monkeypatch.setattr(console, 'MainWindow', window)
    monkeypatch.setattr(console, 'RotatingFileHandler', Mock())
    monkeypatch.setattr(console.logging, 'basicConfig', Mock())

    try:
        arguments = ['--data-dir', str(tmp_path)]
        assert console.main(arguments) == 0
        assert windows == []
        assert load_preference(tmp_path, default=None) is None
        assert not (tmp_path / 'application.lock').exists()

        assert console.main(arguments) == 0
        assert console.main(arguments) == 0
        assert current_language() == 'de'
        assert console.main(arguments + ['--demo']) == 0
        assert console.main(arguments + ['--demo']) == 0
        assert current_language() == 'fr'
        assert prompts == [tmp_path, tmp_path, tmp_path / 'demo']
        assert windows == [(False, 'de'), (False, 'de'), (True, 'fr'), (True, 'fr')]
        assert load_preference(tmp_path) == 'de'
        assert load_preference(tmp_path / 'demo') == 'fr'
    finally:
        set_language('zh_CN')


def test_first_launch_stays_open_when_language_cannot_be_saved(tmp_path, monkeypatch):
    def fail_save(*args):
        raise OSError('Read-only data directory')

    monkeypatch.setattr(language_ui, 'save_preference', fail_save)
    set_language('zh_CN')
    dialog = FirstLaunchLanguageDialog(tmp_path)
    dialog.show()
    try:
        assert [dialog.language_combo.itemData(i) for i in range(dialog.language_combo.count())] == list(LANGUAGES)
        assert dialog.language_combo.currentData() == 'system'
        dialog.continue_button.click()
        assert dialog.isVisible()
        assert 'Read-only data directory' in dialog.status.text()
        assert load_preference(tmp_path, default=None) is None
    finally:
        dialog.close()


@pytest.mark.parametrize('locale,expected', [('en_US','en'),('de_DE','de'),('fr-CA','fr'),
                                           ('ru_RU','ru'),('zh_TW','zh_CN'),('ja_JP','zh_CN')])
def test_system_language_mapping(locale, expected):
    assert resolve_language('system', locale) == expected


def test_catalogs_are_complete_and_preserve_template_fields():
    from warehouse_pc.translations import ui, settings, live, devices
    formatter = Formatter()
    for module in (ui, settings, live, devices):
        assert module.TRANSLATIONS
        for source, translations in module.TRANSLATIONS.items():
            assert len(translations) == 4, source
            fields = {name for _, name, _, _ in formatter.parse(source) if name is not None}
            for text in translations:
                assert isinstance(text, str) and text.strip(), source
                assert {name for _, name, _, _ in formatter.parse(text) if name is not None} == fields, source
    root = Path(__file__).resolve().parents[1] / 'warehouse_pc'
    missing = set()
    for name in ('app', 'language_ui', 'overview_ui', 'warehouse_ui', 'settings_ui', 'history_ui',
                 'maintenance_ui', 'devices_ui', 'registration', 'live_ui', 'silo_ui', 'silo_model'):
        for node in ast.walk(ast.parse((root / f'{name}.py').read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'tr'
                    and node.args and isinstance(node.args[0], ast.Constant)):
                source = node.args[0].value
                if any('\u4e00' <= c <= '\u9fff' for c in str(source)) and source not in catalogs():
                    missing.add(source)
    assert not missing, sorted(missing)


def test_translation_formats_values_without_translating_user_data():
    try:
        set_language('en')
        assert tr('仓房') != '仓房'
        assert tr('用户自定义仓房甲') == '用户自定义仓房甲'
        value = '用户自定义仓房甲 / 2C EE 49 E1 8A AC 11 64'
        assert value in tr('当前语言：{language}', language=value)
    finally:
        set_language('zh_CN')

"""Offline UI translations. Domain identifiers and stored records are never translated."""
import json
import logging
from pathlib import Path
from functools import lru_cache

from PySide6.QtCore import QIODevice, QLibraryInfo, QLocale, QSaveFile, QTranslator
from PySide6.QtWidgets import QApplication

LANGUAGES = {'system': '跟随系统 / System', 'zh_CN': '简体中文', 'en': 'English',
             'de': 'Deutsch', 'fr': 'Français', 'ru': 'Русский'}
_active = 'zh_CN'
_qt_translator = None


def resolve_language(code, system_locale=None):
    if code == 'system':
        code = system_locale or QLocale.system().name()
    prefix = str(code).replace('-', '_').split('_')[0].lower()
    return prefix if prefix in ('en', 'de', 'fr', 'ru') else 'zh_CN'


def current_language():
    return _active


def load_preference(data_dir, default='zh_CN'):
    try:
        value = json.loads((Path(data_dir) / 'ui_preferences.json').read_text(encoding='utf-8')).get('language')
        return value if value in LANGUAGES else default
    except (OSError, ValueError, TypeError, AttributeError):
        return default


def save_preference(data_dir, code):
    if code not in LANGUAGES:
        raise ValueError('Unsupported language')
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = QSaveFile(str(directory / 'ui_preferences.json'))
    payload = json.dumps({'language': code}, ensure_ascii=False).encode('utf-8')
    if not target.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError(target.errorString())
    if target.write(payload) != len(payload):
        target.cancelWriting()
        raise OSError(target.errorString())
    if not target.commit():
        raise OSError(target.errorString())


def set_language(code):
    global _active, _qt_translator
    _active = resolve_language(code)
    QLocale.setDefault(QLocale(_active))
    app = QApplication.instance()
    if app:
        if _qt_translator:
            app.removeTranslator(_qt_translator)
        _qt_translator = QTranslator(app)
        path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        if _qt_translator.load('qtbase_' + _active, path):
            app.installTranslator(_qt_translator)
    return _active


@lru_cache(maxsize=1)
def catalogs():
    from .translations import ui, settings, live, devices
    result = {}
    for module in (ui, settings, live, devices):
        result.update(module.TRANSLATIONS)
    return result


def tr(source, **values):
    text = source
    if _active != 'zh_CN':
        entry = catalogs().get(source)
        if entry:
            text = entry[('en', 'de', 'fr', 'ru').index(_active)]
    if not values:
        return text
    try:
        return text.format(**values)
    except (KeyError, ValueError):
        logging.exception('Invalid translation template: %s', source)
        return source.format(**values)

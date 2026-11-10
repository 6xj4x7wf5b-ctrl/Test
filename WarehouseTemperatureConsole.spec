# Build on the target operating system; Windows releases need Windows Python.
from pathlib import Path
import PySide6
from PySide6.QtCore import QLibraryInfo

project = Path(SPECPATH)
qt_translations = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
qt_destination = Path('PySide6') / qt_translations.relative_to(Path(PySide6.__file__).parent)
language_files = [(str(qt_translations / f'qtbase_{code}.qm'), str(qt_destination))
                 for code in ('zh_CN', 'en', 'de', 'fr', 'ru')
                 if (qt_translations / f'qtbase_{code}.qm').exists()]
analysis = Analysis(
    [str(project / 'launcher.py')],
    pathex=[str(project)],
    binaries=[],
    datas=[(str(project / 'warehouse_pc' / 'assets'), 'warehouse_pc/assets')] + language_files,
    hiddenimports=['PySide6.QtSerialPort', 'openpyxl'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'tkinter'],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True,
    name='WarehouseTemperatureConsole', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False,
)
collection = COLLECT(exe, analysis.binaries, analysis.datas,
                     strip=False, upx=False, name='WarehouseTemperatureConsole')

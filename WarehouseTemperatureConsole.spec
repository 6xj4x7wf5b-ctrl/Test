# Build on the target operating system; Windows releases need Windows Python.
from pathlib import Path

project = Path(SPECPATH)
analysis = Analysis(
    [str(project / 'launcher.py')],
    pathex=[str(project)],
    binaries=[],
    datas=[(str(project / 'warehouse_pc' / 'assets'), 'warehouse_pc/assets')],
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

"""Build on the target OS: an independent, small numbering application."""
from pathlib import Path
import sys

project = Path(SPECPATH)
analysis = Analysis(
    [str(project / 'numbering_launcher.py')],
    pathex=[str(project)],
    binaries=[],
    datas=[(str(project / 'warehouse_pc' / 'assets'), 'warehouse_pc/assets')],
    hiddenimports=['PySide6.QtSerialPort'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
              'tkinter', 'pyqtgraph', 'openpyxl', 'numpy', 'pytest'],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True,
          name='GXHTProbeNumbering', debug=False, bootloader_ignore_signals=False,
          strip=False, upx=False, console=False)
collection = COLLECT(exe, analysis.binaries, analysis.datas,
                     strip=False, upx=False, name='GXHTProbeNumbering')
if sys.platform == 'darwin':
    app = BUNDLE(collection, name='GXHTProbeNumbering.app',
                 bundle_identifier='local.warehouse.gxht-numbering',
                 info_plist={'NSHighResolutionCapable': True})

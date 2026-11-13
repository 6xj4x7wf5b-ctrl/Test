"""Test, freeze and startup-check the independent numbering app on its target OS.

Windows: python tools/build_numbering.py
macOS preview: python tools/build_numbering.py --macos
No sensor hardware is accessed by these checks.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
NAME = 'GXHTProbeNumbering'
EVIDENCE = ROOT / 'build' / 'numbering-release'


def run(command, label, *, env):
    with (EVIDENCE / f'{label}.log').open('w', encoding='utf-8') as output:
        process = subprocess.run([str(x) for x in command], cwd=ROOT, env=env,
                                 stdout=output, stderr=subprocess.STDOUT)
    if process.returncode:
        raise RuntimeError(f'{label} failed ({process.returncode}); see {output.name}')
    print(f'{label}: passed', flush=True)


def smoke(executable, mode, *, env):
    directory = EVIDENCE / '启动验证 data' / mode
    directory.mkdir(parents=True)
    capture = directory / 'window.png'
    command = [str(executable), '--data-dir', str(directory), '--capture', str(capture),
               '--quit-after', '3']
    if mode == 'demo':
        command.append('--demo')
    with (directory / 'process.log').open('w', encoding='utf-8') as output:
        subprocess.run(command, cwd=executable.parent, env=env, stdout=output,
                       stderr=subprocess.STDOUT, timeout=45, check=True)
    if not capture.is_file() or capture.read_bytes()[:8] != b'\x89PNG\r\n\x1a\n':
        raise RuntimeError(f'{mode} did not capture its window')
    databases = sorted(directory.rglob('*.db'))
    if not databases:
        raise RuntimeError(f'{mode} did not initialize its own database')
    for database in databases:
        with closing(sqlite3.connect(database)) as connection:
            if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError(f'{mode} database failed integrity check')
    return {'mode': mode, 'startup_passed': True,
            'databases': [str(p.relative_to(directory)) for p in databases]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--macos', action='store_true', help='Build a local macOS preview bundle')
    args = parser.parse_args()
    if args.macos:
        if sys.platform != 'darwin':
            parser.error('--macos requires macOS')
        target = 'macos-' + platform.machine()
    else:
        if sys.platform != 'win32' or platform.machine().lower() not in ('amd64', 'x86_64'):
            parser.error('Windows x64 Python is required; use --macos for a local preview')
        target = 'windows-x64'
    sys.path.insert(0, str(ROOT))
    from warehouse_numbering import __version__
    archive = ROOT / 'dist' / f'{NAME}-{__version__}-{target}.zip'
    archive.unlink(missing_ok=True)
    archive.with_suffix('.zip.sha256').unlink(missing_ok=True)
    if EVIDENCE.exists():
        shutil.rmtree(EVIDENCE)
    EVIDENCE.mkdir(parents=True)
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               PYINSTALLER_CONFIG_DIR=str(ROOT / 'build' / 'numbering-pyinstaller-cache'))
    for key in ('PYTHONPATH', 'PYTHONHOME', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH'):
        env.pop(key, None)
    test_env = dict(env, QT_QPA_PLATFORM='offscreen')
    tests = sorted(ROOT.glob('tests/test_numbering*.py'))
    if not tests:
        raise RuntimeError('Numbering tests are missing')
    run([sys.executable, '-m', 'pytest', '-q', *tests,
         '--junitxml', EVIDENCE / 'tests.xml'], 'tests', env=test_env)
    run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
         'GXHTProbeNumbering.spec'], 'pyinstaller', env=env)
    package = ROOT / 'dist' / (NAME + '.app' if args.macos else NAME)
    executable = (package / 'Contents' / 'MacOS' / NAME if args.macos
                  else package / (NAME + '.exe'))
    if not executable.is_file():
        raise RuntimeError('The numbering executable was not produced')
    if not args.macos:
        with executable.open('rb') as stream:
            if stream.read(2) != b'MZ':
                raise RuntimeError('Expected Windows PE executable')
            stream.seek(0x3C)
            stream.seek(struct.unpack('<I', stream.read(4))[0])
            if stream.read(6) != b'PE\x00\x00\x64\x86':
                raise RuntimeError('Expected Windows x64 executable')
    smoke_env = dict(env, QT_QPA_PLATFORM='offscreen' if args.macos else 'windows')
    checks = [smoke(executable, mode, env=smoke_env) for mode in ('normal', 'demo')]
    doc_dir = EVIDENCE / f'{NAME}-{__version__}-{target}'
    doc_dir.mkdir()
    shutil.copytree(package, doc_dir / package.name, symlinks=True)
    manual = ROOT / 'docs' / '探头编号工具使用说明.md'
    shutil.copy2(manual, doc_dir / '使用说明.md')
    (doc_dir / '使用说明.txt').write_text(manual.read_text(encoding='utf-8'), encoding='utf-8-sig')
    metadata = {'application': NAME, 'version': __version__, 'target': target,
                'built_at_utc': datetime.now(timezone.utc).isoformat(),
                'startup_checks': checks, 'physical_sensor_tested': False,
                'source_commit': os.environ.get('GITHUB_SHA'),
                'workflow_run': os.environ.get('GITHUB_RUN_ID')}
    (doc_dir / 'build-info.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.macos:
        # ditto preserves executable modes and framework symlinks in the .app.
        subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
                        str(doc_dir), str(archive)], check=True)
    else:
        (doc_dir / '模拟演示.cmd').write_bytes(
            b'@echo off\r\nstart "" "%~dp0GXHTProbeNumbering\\GXHTProbeNumbering.exe" --demo\r\n')
        shutil.make_archive(str(archive.with_suffix('')), 'zip',
                            root_dir=doc_dir.parent, base_dir=doc_dir.name)
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{digest}  {archive.name}\n', encoding='ascii')
    print(f'Built and startup-checked: {archive}\nSHA256: {digest}', flush=True)


if __name__ == '__main__':
    main()

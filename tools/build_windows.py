"""Build and verify a Windows x64 portable release on Windows Python.

Run from the repository root: python tools/build_windows.py
The same entry point is used by PowerShell and GitHub Actions.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys
import sysconfig
import tomllib
import venv
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "build" / "windows-release"
APP_NAME = "WarehouseTemperatureConsole"


def run_logged(command, name, *, env):
    print(f"\n[{name}]", flush=True)
    with (EVIDENCE / f"{name}.log").open("w", encoding="utf-8") as log:
        with subprocess.Popen(
            [str(arg) for arg in command], cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        ) as process:
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
            return_code = process.wait()
            if return_code != 0:
                raise RuntimeError(f"{name} failed with exit code {return_code} "
                                   f"(0x{return_code & 0xFFFFFFFF:08X}); see {log.name}")


def check_windows_exe(path):
    with path.open("rb") as executable:
        if executable.read(2) != b"MZ":
            raise RuntimeError("The build did not produce a Windows executable")
        executable.seek(0x3C)
        executable.seek(struct.unpack("<I", executable.read(4))[0])
        if executable.read(4) != b"PE\0\0" or executable.read(2) != b"\x64\x86":
            raise RuntimeError("The executable is not Windows x64 (AMD64)")


def smoke_check(executable, mode, *, env):
    # Spaces and Chinese text exercise the target machine's path handling.
    directory = EVIDENCE / "启动检查 data" / mode
    directory.mkdir(parents=True)
    capture = directory / "window.png"
    command = [str(executable), "--data-dir", str(directory),
               "--capture", str(capture), "--quit-after", "10"]
    if mode == "demo":
        command.append("--demo")
    print(f"\n[Starting packaged EXE: {mode}]", flush=True)
    smoke_env = dict(env, QT_QPA_PLATFORM="windows")
    with (directory / "process.log").open("w", encoding="utf-8") as log:
        subprocess.run(command, cwd=executable.parent, env=smoke_env,
                       stdout=log, stderr=subprocess.STDOUT, check=True, timeout=90)
    if not capture.is_file() or capture.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError(f"{mode} startup did not capture its main window")
    database = directory / ("demo/warehouse.db" if mode == "demo" else "warehouse.db")
    if not database.is_file():
        raise RuntimeError(f"{mode} startup did not create its database")
    with closing(sqlite3.connect(database)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"{mode} database integrity check failed")
        devices = connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        samples = connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        if mode == "demo":
            if devices != 2 or samples == 0:
                raise RuntimeError("Demo did not collect data from both simulated devices")
            sources = {row[0] for row in connection.execute("SELECT DISTINCT source FROM samples")}
            sampled_devices = connection.execute("SELECT COUNT(DISTINCT device_id) FROM samples").fetchone()[0]
            if sources != {"simulator"} or sampled_devices != 2:
                raise RuntimeError("Demo data source or multi-device collection is incorrect")
            for device_id, grain_count, ambient_count in connection.execute(
                "SELECT id, grain_count, ambient_count FROM devices"
            ):
                expected = {(kind, point) for kind, count in (("grain", grain_count), ("ambient", ambient_count))
                            for point in range(1, count + 1)}
                valid = set(connection.execute(
                    "SELECT DISTINCT kind, point FROM samples WHERE device_id=? "
                    "AND status='ok' AND temp IS NOT NULL "
                    "AND (kind='grain' OR humidity IS NOT NULL)", (device_id,)
                ))
                if valid != expected:
                    raise RuntimeError(f"Demo device {device_id} did not collect all valid measurements")
        elif devices != 0 or samples != 0:
            raise RuntimeError("Normal startup unexpectedly contains seeded data")
    return {"mode": mode, "passed": True, "devices": devices, "samples": samples}


def main():
    if sys.platform != "win32" or sysconfig.get_platform() != "win-amd64":
        raise SystemExit("Build requires Windows x64 Python. On macOS, use the Windows Build GitHub Actions workflow.")
    if sys.version_info[:2] not in {(3, 11), (3, 12)}:
        raise SystemExit("Use Python 3.11 or 3.12 (64-bit); GitHub Actions uses 3.11.")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    package = ROOT / "dist" / APP_NAME
    archive = ROOT / "dist" / f"{APP_NAME}-{version}-windows-x64.zip"
    checksum = archive.with_suffix(".zip.sha256")
    # Remove only this script's previous results, so a failed run cannot leave
    # an old ZIP looking like a successful release of the current source.
    for old_file in (archive, checksum):
        old_file.unlink(missing_ok=True)
    if EVIDENCE.exists():
        shutil.rmtree(EVIDENCE)
    EVIDENCE.mkdir(parents=True)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", QT_API="pyside6",
               PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_PROGRESS_BAR="off")
    for key in ("PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH"):
        env.pop(key, None)
    environment = ROOT / ".venv-windows"
    print("Creating isolated Windows build environment...", flush=True)
    venv.EnvBuilder(with_pip=True, clear=True).create(environment)
    python = environment / "Scripts" / "python.exe"
    run_logged([python, "-m", "pip", "install", "-c", "requirements-windows-build.txt", ".[dev]"], "dependencies", env=env)
    run_logged([python, "-m", "pip", "check"], "dependency-check", env=env)
    run_logged([python, "-m", "pytest", "-q", "--junitxml", EVIDENCE / "test-results.xml"], "tests", env=env)
    run_logged([python, "-m", "PyInstaller", "--noconfirm", "--clean", "WarehouseTemperatureConsole.spec"], "pyinstaller", env=env)
    executable = package / f"{APP_NAME}.exe"
    check_windows_exe(executable)
    smoke = [smoke_check(executable, mode, env=env) for mode in ("normal", "demo")]
    freeze = subprocess.check_output([str(python), "-m", "pip", "freeze"], cwd=ROOT, env=env, text=True, encoding="utf-8")
    (package / "build-requirements.txt").write_text(freeze, encoding="utf-8")
    suites = ET.parse(EVIDENCE / "test-results.xml").getroot().iter("testsuite")
    totals = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.attrib.get(key, 0))
    metadata = {
        "version": version, "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "target": "windows-x64", "python": sys.version, "source_commit": os.environ.get("GITHUB_SHA"),
        "workflow_run": os.environ.get("GITHUB_RUN_ID"), "tests": totals,
        "windows_binary_built": True, "startup_checks": smoke, "physical_device_tested": False,
    }
    (package / "build-info.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(ROOT / "release" / "WINDOWS_README.txt", package / "使用说明.txt")
    (package / "Demo.cmd").write_bytes(b'@echo off\r\nstart "" "%~dp0WarehouseTemperatureConsole.exe" --demo\r\n')
    shutil.make_archive(str(archive.with_suffix("")), "zip", root_dir=package.parent, base_dir=package.name)
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(f"\nBuilt and startup-checked: {archive}\nSHA256: {digest}", flush=True)


if __name__ == "__main__":
    main()

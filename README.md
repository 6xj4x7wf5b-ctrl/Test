# GXHT3W Probe Numbering 0.1.0

Independent offline tool for preparing one GXHT3W probe at a time. TH is the group number, TL is the point number (1–254). Requires the matching STM32 numbering fixture firmware; production collection firmware cannot program probes through these commands.

Run from source using Python 3.11 or 3.12:

```sh
python -m pip install -c requirements-windows-build.txt '.[dev]'
python -m warehouse_numbering --demo
```

Omit `--demo` to connect to the actual numbering fixture. Default fixture settings: 1200 / 8N1, address 1. Records and simulation data are isolated. Never treat a simulated verification as a physical sensor test.

Windows x64 build: `python tools/build_numbering.py`. The workflow runs tests, freezes the app, then checks normal and demo startup on Windows before archiving it. macOS preview: `python tools/build_numbering.py --macos`.

See [中文使用说明](docs/探头编号工具使用说明.md). The `warehouse_pc` directory contains shared serial codecs, styling and layout helpers only. This source package does not contain the daily warehouse collection application, field databases or logs.

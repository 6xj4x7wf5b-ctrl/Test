$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
python tools/build_windows.py
if ($LASTEXITCODE -ne 0) { throw 'Windows release build failed; see build/windows-release and console output.' }

#Requires -Version 5.1
# Starts the dev stack (the Procfile's services) under scripts/devstack.py.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\start.ps1
#
# devstack.py replaced honcho (2026-09-30).  When a service died, honcho killed only the
# other services' cmd.exe wrappers, then waited forever for the uv -> python chains under
# them to close their output pipes, so it never exited and this script's cleanup never ran.
# devstack.py holds every service in a kill-on-close job: when any service exits, on Ctrl+C,
# or if devstack.py itself is killed, the kernel ends every process in the stack.  No
# cleanup is needed here.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Always run from the project root so devstack.py finds the Procfile.
Set-Location (Split-Path -Parent $PSScriptRoot)

# uv is usually installed in the user-local bin directory.
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"

# Force UTF-8 for devstack.py and every Python child.  When stdout is a pipe or a file (an
# IDE pane, a redirect) Python falls back to the ANSI code page, which lacks characters the
# services print: Vite's startup banner prints U+279C.
$env:PYTHONUTF8       = '1'
$env:PYTHONIOENCODING = 'utf-8'

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[start.ps1] ERROR: 'uv' not found on PATH. See https://docs.astral.sh/uv/" `
        -ForegroundColor Red
    exit 1
}

Write-Host "[start.ps1] Starting RetroStation services..."
& uv run python scripts\devstack.py
exit $LASTEXITCODE

#Requires -Version 5.1
# Wrapper for honcho start that ensures all child processes are cleaned up on exit.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\start.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Always run from the project root so honcho can find the Procfile.
Set-Location (Split-Path -Parent $PSScriptRoot)

# Add user-local paths and the Python Scripts directory to PATH.
# Get-ChildItem returns an empty array (not an error) when no version matches,
# so this is safe when no user-level Python installation exists.
$pythonScriptsDirs = @(
    Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python*\Scripts" `
        -Directory -ErrorAction SilentlyContinue
)
$extraPaths = [System.Collections.Generic.List[string]]@("$env:USERPROFILE\.local\bin")
if ($pythonScriptsDirs.Count -gt 0) {
    $extraPaths.Add($pythonScriptsDirs[0].FullName)
}
$env:PATH = ($extraPaths + $env:PATH) -join ';'

# Abort early with a clear message if honcho is not installed.
if (-not (Get-Command honcho -ErrorAction SilentlyContinue)) {
    Write-Host "[start.ps1] ERROR: 'honcho' not found on PATH. Install it with: pip install honcho" `
        -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# Stop-ProcessTree
#   Kills honcho and every process it spawned via taskkill /F /T, which walks
#   the full Windows process tree — no signal propagation required.
#
#   Accepts a plain [int] PID rather than a Process object so the function
#   never blocks on HasExited.  The caller must snapshot $honchoProcess.Id
#   immediately after Start-Process, before the Ctrl+C race can flip
#   HasExited to $true.  taskkill returns exit code 128 when the PID is
#   already gone; that is silently ignored — the tree is already clean.
# ---------------------------------------------------------------------------
function Stop-ProcessTree {
    param([int]$RootPid)
    if ($RootPid -le 0) { return }
    Write-Host "[start.ps1] Killing process tree (PID $RootPid)..."
    # /F = force-terminate, /T = include all child processes recursively.
    # Redirect stderr so a "not found" message from taskkill doesn't surface.
    & taskkill.exe /F /T /PID $RootPid 2>$null
    # Do not check $LASTEXITCODE here: exit code 128 means the PID was already
    # gone (honcho exited cleanly from Ctrl+C before we got here), which is fine.
}

# ---------------------------------------------------------------------------
# Clear-OccupiedPorts
#   Sweeps any orphan processes still listening on the service ports.
#   Uses Get-NetTCPConnection instead of parsing netstat output.
#
#   Ports covered:
#     8000 — uvicorn / FastAPI (backend.run_server)
#     5173 — Vite dev server  (frontend, per vite.config.ts)
# ---------------------------------------------------------------------------
function Clear-OccupiedPorts {
    foreach ($port in @(8000, 5173)) {
        $conn = Get-NetTCPConnection -LocalPort $port -State Listen `
            -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($conn -and $conn.OwningProcess -ne 0) {
            Write-Host "[start.ps1] Killing orphan on port $port (PID $($conn.OwningProcess))"
            & taskkill.exe /F /PID $conn.OwningProcess 2>$null
        }
    }
}

# ---------------------------------------------------------------------------
# Main
#   Start honcho, then poll HasExited every 200 ms.  Polling (rather than
#   calling WaitForExit()) lets Ctrl+C interrupt Start-Sleep and unwind into
#   the finally block, which guarantees cleanup on every exit path.
#
#   IMPORTANT: capture $honchoPid as a plain [int] immediately after
#   Start-Process.  By the time the finally block runs, Ctrl+C has already
#   been broadcast to honcho (it shares our console via -NoNewWindow), so
#   honcho may have already exited and $honchoProcess.HasExited may be $true.
#   Using the raw PID lets Stop-ProcessTree call taskkill unconditionally,
#   which walks the child tree even when the root PID is gone.
# ---------------------------------------------------------------------------
$honchoProcess = $null
$honchoPid     = 0          # captured as [int] before any race can occur
try {
    Write-Host "[start.ps1] Starting RetroStation services..."
    $honchoProcess = Start-Process `
        -FilePath     'honcho' `
        -ArgumentList 'start' `
        -NoNewWindow `
        -PassThru
    $honchoPid = $honchoProcess.Id  # snapshot before Ctrl+C can flip HasExited

    while (!$honchoProcess.HasExited) {
        Start-Sleep -Milliseconds 200
    }
}
finally {
    Write-Host ""
    Write-Host "[start.ps1] Shutting down..."

    Stop-ProcessTree $honchoPid     # always runs; never short-circuits on HasExited
    Start-Sleep -Seconds 1
    Clear-OccupiedPorts

    Write-Host "[start.ps1] All processes stopped."
}


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
# ---------------------------------------------------------------------------
function Stop-ProcessTree {
    param([System.Diagnostics.Process]$Process)
    if ($null -eq $Process -or $Process.HasExited) { return }
    Write-Host "[start.ps1] Killing process tree (PID $($Process.Id))..."
    & taskkill.exe /F /T /PID $Process.Id 2>$null
}

# ---------------------------------------------------------------------------
# Clear-OccupiedPorts
#   Sweeps any orphan processes still listening on the service ports.
#   Uses Get-NetTCPConnection instead of parsing netstat output.
# ---------------------------------------------------------------------------
function Clear-OccupiedPorts {
    foreach ($port in @(8000, 3000)) {
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
# ---------------------------------------------------------------------------
$honchoProcess = $null
try {
    Write-Host "[start.ps1] Starting RetroStation services..."
    $honchoProcess = Start-Process `
        -FilePath    'honcho' `
        -ArgumentList 'start' `
        -NoNewWindow `
        -PassThru

    while (!$honchoProcess.HasExited) {
        Start-Sleep -Milliseconds 200
    }
}
finally {
    Write-Host ""
    Write-Host "[start.ps1] Shutting down..."

    Stop-ProcessTree  $honchoProcess
    Start-Sleep -Seconds 1
    Clear-OccupiedPorts

    Write-Host "[start.ps1] All processes stopped."
}


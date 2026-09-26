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

# Force UTF-8 for honcho and every Python child.  When stdout is a pipe or a
# file (an IDE pane, a redirect) Python falls back to the ANSI code page, and
# honcho dies with UnicodeEncodeError the first time it relays a character
# cp1252 lacks: Vite's startup banner prints U+279C.  Honcho does not exit
# cleanly when that happens, and its children keep running.
$env:PYTHONUTF8       = '1'
$env:PYTHONIOENCODING = 'utf-8'

# Abort early with a clear message if honcho is not installed.
if (-not (Get-Command honcho -ErrorAction SilentlyContinue)) {
    Write-Host "[start.ps1] ERROR: 'honcho' not found on PATH. Install it with: pip install honcho" `
        -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# Invoke-Taskkill
#   Runs taskkill.exe and never throws.  Under $ErrorActionPreference='Stop',
#   Windows PowerShell 5.1 turns a native command's redirected stderr into a
#   terminating NativeCommandError, so taskkill's "process not found" line
#   aborted the finally block and skipped every cleanup step after it.
#   The function-local preference below applies to this call only.
# ---------------------------------------------------------------------------
function Invoke-Taskkill {
    $ErrorActionPreference = 'SilentlyContinue'
    & taskkill.exe @args 2>$null | Out-Null
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
#   already gone; that is ignored here, and Stop-Snapshot then reaches any
#   services the dead root left behind.
# ---------------------------------------------------------------------------
function Stop-ProcessTree {
    param([int]$RootPid)
    if ($RootPid -le 0) { return }
    Write-Host "[start.ps1] Killing process tree (PID $RootPid)..."
    # /F = force-terminate, /T = include all child processes recursively.
    Invoke-Taskkill /F /T /PID $RootPid
}

# ---------------------------------------------------------------------------
# Get-DescendantSnapshot
#   Returns every live descendant of RootPid as @{Id; Created} records.
#
#   taskkill /T can only walk the tree from a live root: once honcho itself
#   has died (a crash, or its own Ctrl+C handling finishing first), each
#   service's cmd.exe -> uv -> python chain becomes an orphan island that
#   /T never reaches.  The huey worker listens on no port, so
#   Clear-OccupiedPorts misses it too, and it keeps running against the DB
#   with a dead stdout.  Snapshotting the tree while honcho is alive gives
#   the finally block the PIDs it needs.  CreationDate guards against PID
#   reuse: a PID is only killed if the process holding it is the same one.
# ---------------------------------------------------------------------------
function Get-DescendantSnapshot {
    param([int]$RootPid)
    $all = @(Get-CimInstance Win32_Process -Property ProcessId, ParentProcessId, CreationDate)
    $byParent = @{}
    foreach ($p in $all) {
        $key = [int]$p.ParentProcessId
        if (-not $byParent.ContainsKey($key)) { $byParent[$key] = @() }
        $byParent[$key] += $p
    }
    $found = [System.Collections.Generic.List[object]]::new()
    $seen  = [System.Collections.Generic.HashSet[int]]::new()
    $queue = [System.Collections.Generic.Queue[int]]::new()
    [void]$seen.Add($RootPid)
    $queue.Enqueue($RootPid)
    while ($queue.Count -gt 0) {
        $parentId = $queue.Dequeue()
        if (-not $byParent.ContainsKey($parentId)) { continue }
        foreach ($child in $byParent[$parentId]) {
            $childId = [int]$child.ProcessId
            if (-not $seen.Add($childId)) { continue }
            $found.Add(@{ Id = $childId; Created = $child.CreationDate })
            $queue.Enqueue($childId)
        }
    }
    return , $found
}

# ---------------------------------------------------------------------------
# Stop-Snapshot
#   Kills each snapshotted process that is still the same process (same PID
#   and creation time).  /T also takes any children it spawned since.
# ---------------------------------------------------------------------------
function Stop-Snapshot {
    param($Snapshot)
    if ($null -eq $Snapshot) { return }
    foreach ($entry in $Snapshot) {
        $live = Get-CimInstance Win32_Process -Filter "ProcessId = $($entry.Id)" `
            -Property CreationDate -ErrorAction SilentlyContinue
        if ($live -and $live.CreationDate -eq $entry.Created) {
            Write-Host "[start.ps1] Killing orphaned service process (PID $($entry.Id))"
            Invoke-Taskkill /F /T /PID $entry.Id
        }
    }
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
            Invoke-Taskkill /F /PID $conn.OwningProcess
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
#   Using the raw PID lets Stop-ProcessTree call taskkill unconditionally.
#   taskkill /T cannot reach the services once honcho is gone, so the loop
#   also keeps a snapshot of honcho's descendants for the finally block:
#   every second for the first 30 s (services start, and a startup crash
#   happens, in that window), then every 5 s.
# ---------------------------------------------------------------------------
$honchoProcess = $null
$honchoPid     = 0          # captured as [int] before any race can occur
$descendants   = $null
try {
    Write-Host "[start.ps1] Starting RetroStation services..."
    $honchoProcess = Start-Process `
        -FilePath     'honcho' `
        -ArgumentList 'start' `
        -NoNewWindow `
        -PassThru
    $honchoPid = $honchoProcess.Id  # snapshot before Ctrl+C can flip HasExited

    $tick = 0
    while (!$honchoProcess.HasExited) {
        $refreshEvery = if ($tick -lt 150) { 5 } else { 25 }
        if ($tick % $refreshEvery -eq 0) {
            $descendants = Get-DescendantSnapshot $honchoPid
        }
        Start-Sleep -Milliseconds 200
        $tick++
    }
}
finally {
    Write-Host ""
    Write-Host "[start.ps1] Shutting down..."

    Stop-ProcessTree $honchoPid     # always runs; never short-circuits on HasExited
    Stop-Snapshot $descendants      # services orphaned if honcho died first
    Start-Sleep -Seconds 1
    Clear-OccupiedPorts

    Write-Host "[start.ps1] All processes stopped."
}


#!/usr/bin/env bash
# Wrapper for honcho start that ensures all child processes are cleaned up on exit.
# Usage: bash scripts/start.sh

set -euo pipefail

# FIX (Issue 5): Always run from the project root regardless of where the script
# is invoked from, so honcho can find the Procfile.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

# FIX (Issue 2): Enable nullglob so the glob produces an empty array (not a
# literal glob string) when no Python installation is found.
shopt -s nullglob
PYTHON_SCRIPTS=("$HOME"/AppData/Local/Programs/Python/Python*/Scripts)
shopt -u nullglob

# Build PATH; ${PYTHON_SCRIPTS[0]:-} is safely empty when the array is empty.
export PATH="$HOME/.local/bin:${PYTHON_SCRIPTS[0]:-}:$PATH"

# FIX (Issue 4): Abort early with a clear message if honcho is not installed.
if ! command -v honcho &>/dev/null; then
    echo "[start.sh] ERROR: 'honcho' not found on PATH. Install it with: pip install honcho" >&2
    exit 1
fi

# Translate a MSYS PID to a Windows PID that taskkill.exe understands.
msys_to_winpid() {
    local msys_pid="$1"
    if [ -f "/proc/$msys_pid/winpid" ]; then
        cat "/proc/$msys_pid/winpid"
    else
        echo "$msys_pid"
    fi
}

cleanup() {
    echo ""
    echo "[start.sh] Shutting down..."

    # Kill honcho's entire process tree via Windows taskkill.
    if [ -n "${HONCHO_PID:-}" ]; then
        local winpid
        winpid=$(msys_to_winpid "$HONCHO_PID")

        if command -v taskkill.exe &>/dev/null; then
            echo "[start.sh] Killing process tree (Windows PID $winpid)..."
            # MSYS_NO_PATHCONV prevents Git Bash from mangling /F into a filepath.
            MSYS_NO_PATHCONV=1 taskkill.exe /F /T /PID "$winpid" &>/dev/null || true
        else
            # Non-Windows: honcho was launched with setsid, so PGID == HONCHO_PID.
            # Send SIGTERM to the entire process group, then poll for up to 2 s and
            # SIGKILL any survivors.  This is reliable regardless of whether honcho
            # itself propagates the signal to its children.
            kill -TERM -- -"$HONCHO_PID" 2>/dev/null || true
            local deadline=$(( $(date +%s) + 2 ))
            while kill -0 -- -"$HONCHO_PID" 2>/dev/null; do
                [ "$(date +%s)" -ge "$deadline" ] && break
                sleep 0.1
            done
            kill -KILL -- -"$HONCHO_PID" 2>/dev/null || true
        fi
    fi

    sleep 1

    # Sweep any orphans still holding our ports.
    if command -v netstat.exe &>/dev/null; then
        for port in 8000 3000; do
            local pid
            pid=$(netstat.exe -ano 2>/dev/null \
                | grep ":${port} " \
                | grep "LISTENING" \
                | awk '{print $NF}' \
                | head -1)
            if [ -n "$pid" ] && [ "$pid" != "0" ]; then
                echo "[start.sh] Killing orphan on port $port (PID $pid)"
                MSYS_NO_PATHCONV=1 taskkill.exe /F /PID "$pid" &>/dev/null || true
            fi
        done
    fi

    echo "[start.sh] All processes stopped."
}

# Trap only EXIT so cleanup() runs exactly once for every exit path.
# INT and TERM traps record the signal name, then call exit with the conventional
# code (128 + signal number): 130 for SIGINT (Ctrl+C), 143 for SIGTERM.
# Calling exit from those traps fires the EXIT trap, which runs cleanup().
_SIGNAL=""
trap 'cleanup'              EXIT
trap '_SIGNAL=INT;  exit 130' INT
trap '_SIGNAL=TERM; exit 143' TERM

echo "[start.sh] Starting RetroStation services..."
# On non-Windows, launch honcho under setsid so it becomes the leader of a new
# process group (PGID == its own PID).  That makes "kill -- -HONCHO_PID" in
# cleanup() target the whole tree atomically.  On Windows, taskkill /F /T
# already walks the tree for us, so setsid is unnecessary (and unavailable).
if command -v taskkill.exe &>/dev/null; then
    honcho start &
else
    setsid honcho start &
fi
HONCHO_PID=$!

# Wait for honcho; 'wait' returns when honcho exits or we get a signal.
wait "$HONCHO_PID" 2>/dev/null || true

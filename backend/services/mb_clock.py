"""One machine-wide clock for MusicBrainz request pacing.

MusicBrainz limits requests per IP address, so the cadence has to hold per machine,
not per process: the API process and the Huey worker share this clock. The last-call
time is the mtime of ``CLOCK_PATH``; a file lock serialises everyone who reads and
updates it.

The lock is a sibling ``.lock`` file, not the clock file itself, because filelock's
Windows backend unlinks the lock file on release, which would erase the mtime.

``time.time()`` is used rather than a monotonic clock because the value is compared
with ``st_mtime``, which is wall-clock time.

The sleep is capped at one cadence, so a clock file stamped in the future (system clock
stepped back, file restored) cannot hold the lock for long; ``touch`` then resets it.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from filelock import FileLock

CLOCK_PATH = Path(tempfile.gettempdir()) / "retrostation-musicbrainz.clock"
CADENCE_S = 1.1


def wait_turn(clock: Path = CLOCK_PATH, cadence_s: float = CADENCE_S) -> None:
    """Block until ``cadence_s`` has passed since the last call on this machine."""
    with FileLock(clock.with_suffix(".lock")):
        last = clock.stat().st_mtime if clock.exists() else 0.0
        time.sleep(min(cadence_s, max(0.0, last + cadence_s - time.time())))
        clock.touch()

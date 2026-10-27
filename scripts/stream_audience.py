"""The stream's audience from the session engine logs (R18): how many listeners at once, at peak
and typically, and which sessions fell behind real time.

Each log in ``var/stream/logs`` is one tune-in: from its first timestamped line to the file's
last write (engines are killed at session end, so there is no end marker). The app prunes the
logs to the newest 50 at each start (D35) and logs no tune-in event, so the figures cover only
the period the logs reach, which the report states. A 30-day audience is a known gap until
tune-ins are logged.

    uv run python scripts/stream_audience.py [var/stream/logs]
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_LOGS = Path("var/stream/logs")
_STAMP = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) ")
_CATCHUP = re.compile(r"we must catchup ([\d.]+) seconds")


@dataclass(frozen=True)
class Session:
    name: str
    start: datetime
    end: datetime
    catchups: tuple[float, ...]  # seconds behind, one per warning


def sessions(logs: Path) -> list[Session]:
    """Every log with a timestamped line, oldest first."""
    found = []
    for log in logs.glob("*.log"):
        start, catchups = None, []
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            stamp = _STAMP.match(line)
            if stamp is None:
                continue
            if start is None:
                start = datetime.strptime(stamp.group(1), "%Y/%m/%d %H:%M:%S")
            late = _CATCHUP.search(line)
            if late:
                catchups.append(float(late.group(1)))
        if start is not None:
            end = datetime.fromtimestamp(log.stat().st_mtime)
            found.append(Session(log.stem, start, end, tuple(catchups)))
    return sorted(found, key=lambda s: s.start)


@dataclass(frozen=True)
class Audience:
    sessions: int
    first: datetime
    last: datetime
    peak: int
    peak_at: datetime
    typical: int  # the number of listeners held longest while anyone listened
    time_at: dict[int, timedelta]  # time spent at each number of listeners


def audience(found: Sequence[Session]) -> Audience:
    """Concurrent listeners over the period ``found`` covers."""
    edges = sorted([(s.start, 1) for s in found] + [(s.end, -1) for s in found])
    level, peak, peak_at, previous = 0, 0, edges[0][0], edges[0][0]
    time_at: dict[int, timedelta] = {}
    for at, step in edges:
        time_at[level] = time_at.get(level, timedelta()) + (at - previous)
        level += step
        previous = at
        if level > peak:
            peak, peak_at = level, at
    listening = {n: t for n, t in time_at.items() if n > 0}
    return Audience(
        sessions=len(found),
        first=found[0].start,
        last=max(s.end for s in found),
        peak=peak,
        peak_at=peak_at,
        typical=max(listening, key=lambda n: listening[n]) if listening else 0,
        time_at=time_at,
    )


def main(argv: Sequence[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    logs = Path(argv[0]) if argv else DEFAULT_LOGS
    found = sessions(logs)
    if not found:
        print(f"no session logs in {logs}")
        return 1
    report = audience(found)
    span = report.last - report.first
    print(f"{report.sessions} sessions, {report.first:%Y-%m-%d %H:%M} to "
          f"{report.last:%Y-%m-%d %H:%M} ({span.days} days {span.seconds // 3600} h; the logs "
          f"reach no further: no tune-in is logged, and the app keeps the newest 50)")  # fmt: skip
    print(
        f"peak: {report.peak} at once, {report.peak_at:%Y-%m-%d %H:%M}; typical: {report.typical}"
    )
    total = sum(report.time_at.values(), timedelta())
    for n in sorted(report.time_at):
        print(f"  {n} at once: {report.time_at[n].total_seconds() / 3600:7.1f} h "
              f"({report.time_at[n] / total:.1%})")  # fmt: skip
    late = [s for s in found if s.catchups]
    print(f"sessions that fell behind real time: {len(late)} of {len(found)}")
    for s in late:
        print(f"  {s.start:%Y-%m-%d %H:%M} {s.name[:8]}: {len(s.catchups)} catch-up warnings, "
              f"worst {max(s.catchups):.2f} s")  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

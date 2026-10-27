"""The stream's audience from the session engine logs (R18 audience figures): each log is one
tune-in, from its first timestamped line to the file's last write (engines are killed at session
end, so no end marker). The logs are pruned to the newest 50 at each app start, so the figures
cover only the period the logs reach; the result says how far that is."""

from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture(scope="module")
def audience() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    return importlib.import_module("stream_audience")


def write_log(
    folder: Path, name: str, start: str, end: str, catchups: tuple[float, ...] = ()
) -> None:
    lines = [f"{start} >>> LOG START", f"{start} [main:3] Liquidsoap 2.4.5"]
    lines += [
        f"{start} [clock.output.harbor:2] Latency is too high: we must catchup {c:.2f} seconds!"
        for c in catchups
    ]
    path = folder / f"{name}.log"
    path.write_text("\n".join(lines) + "\nRS FETCHED session=x seq=1\n", encoding="utf-8")
    ended = datetime.strptime(end, "%Y/%m/%d %H:%M:%S").timestamp()
    os.utime(path, (ended, ended))


def test_peak_and_typical_listeners_from_overlapping_sessions(
    audience: ModuleType, tmp_path: Path
) -> None:
    write_log(tmp_path, "a", "2026/10/20 10:00:00", "2026/10/20 11:00:00")
    write_log(tmp_path, "b", "2026/10/20 10:30:00", "2026/10/20 10:40:00")
    write_log(tmp_path, "c", "2026/10/21 09:00:00", "2026/10/21 09:30:00")

    report = audience.audience(audience.sessions(tmp_path))

    assert report.sessions == 3
    assert report.peak == 2
    assert report.peak_at == datetime(2026, 10, 20, 10, 30)
    assert report.typical == 1  # one listener for 80 of the 90 minutes anyone listened
    assert report.first == datetime(2026, 10, 20, 10, 0)
    assert report.last == datetime(2026, 10, 21, 9, 30)


def test_catch_up_warnings_are_counted_per_session(audience: ModuleType, tmp_path: Path) -> None:
    write_log(tmp_path, "a", "2026/10/20 10:00:00", "2026/10/20 10:10:00", (0.4, 2.5))
    write_log(tmp_path, "b", "2026/10/20 11:00:00", "2026/10/20 11:10:00")

    [late] = [s for s in audience.sessions(tmp_path) if s.catchups]

    assert late.name == "a"
    assert late.catchups == (0.4, 2.5)


def test_a_log_with_no_timestamped_line_is_left_out(audience: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "empty.log").write_text("RS FETCHED session=x seq=1\n", encoding="utf-8")
    write_log(tmp_path, "a", "2026/10/20 10:00:00", "2026/10/20 10:10:00")

    assert [s.name for s in audience.sessions(tmp_path)] == ["a"]

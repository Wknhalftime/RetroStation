"""Acceptance tests: the Library silent-files report's use case (S6).

silent_file_report keeps the files with a reason, silent files first, each group in path
order, and counts both groups. The fake answers rows with no reason too, so the filter is
the service's.

The service, the port and the fake are imported inside each test, never at module level: until
the build they do not exist, and a module-level ImportError would stop pytest collecting every
test in tests/services/streaming, not just these.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from backend.domain import streaming


def entry(path: str, **overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "file_id": uuid4(),
        "path": path,
        "artist": None,
        "title": None,
        "album": None,
        "duration_ms": 200_000,
        "file_size": 30_000_000,
        "audio_silent": False,
        "cued_span_ms": 180_000,
    }
    return streaming.SilentFileEntry(**{**fields, **overrides})


def test_silent_files_come_first_then_the_nearly_silent_each_in_path_order() -> None:
    from backend.services.streaming.silent_files import silent_file_report
    from tests.fakes.silent_files import FakeSilentFileReportRepository

    repo = FakeSilentFileReportRepository(
        [
            entry("D:/m/b.flac", file_size=1_000),
            entry("D:/m/d.flac", audio_silent=True, file_size=1_000, cued_span_ms=None),
            entry("D:/m/a.flac", cued_span_ms=1_000),
            entry("D:/m/c.flac", audio_silent=True, cued_span_ms=None),
        ]
    )
    report = silent_file_report(repo)
    assert [f.path for f in report.files] == [
        "D:/m/c.flac",
        "D:/m/d.flac",
        "D:/m/a.flac",
        "D:/m/b.flac",
    ]
    assert (report.silent, report.nearly_silent) == (2, 2)


def test_a_file_with_no_reason_is_left_out() -> None:
    from backend.services.streaming.silent_files import silent_file_report
    from tests.fakes.silent_files import FakeSilentFileReportRepository

    report = silent_file_report(FakeSilentFileReportRepository([entry("D:/m/song.flac")]))
    assert (report.files, report.silent, report.nearly_silent) == ((), 0, 0)


def test_the_fake_is_concrete() -> None:
    from backend.repositories.silent_files import SilentFileReportRepository
    from tests.fakes.silent_files import FakeSilentFileReportRepository

    assert isinstance(FakeSilentFileReportRepository(), SilentFileReportRepository)

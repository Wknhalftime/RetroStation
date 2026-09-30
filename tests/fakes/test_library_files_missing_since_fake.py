"""The fake stamps and clears missing_since as PG does (spec C1), on an injected clock."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import uuid4

from backend.domain.library import LibraryFile
from tests.fakes.library_files import FakeLibraryFileRepository

_T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
_T1 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _file(path: str) -> LibraryFile:
    return LibraryFile(id=uuid4(), file_path=path, format="flac")


def test_fake_mark_missing_stamps_the_first_time_only() -> None:
    now = [_T0]
    repo = FakeLibraryFileRepository(clock=lambda: now[0])
    row = repo.upsert(_file("/m/a.flac"))

    repo.mark_missing("/m/a.flac")
    now[0] = _T1
    repo.mark_missing("/m/a.flac")

    got = repo.get_by_id(row.id)
    assert got is not None and got.missing_since == _T0


def test_fake_relocate_clears_missing_since() -> None:
    repo = FakeLibraryFileRepository(clock=lambda: _T0)
    row = repo.upsert(_file("/m/a.flac"))
    repo.mark_missing("/m/a.flac")

    repo.relocate(row.id, "/m/b.flac")

    got = repo.get_by_id(row.id)
    assert got is not None and got.missing_since is None


def test_fake_upsert_of_a_missing_row_clears_missing_since() -> None:
    # What _restore_reappeared_file does: upsert the stored row, time and all.
    repo = FakeLibraryFileRepository(clock=lambda: _T0)
    repo.upsert(_file("/m/a.flac"))
    repo.mark_missing("/m/a.flac")
    stale = repo.get_by_path("/m/a.flac")
    assert stale is not None and stale.missing_since == _T0

    got = repo.upsert(dataclasses.replace(stale))

    assert got.missing_since is None

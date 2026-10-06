"""The library-file fake mirrors D11 and the re-check wave (spec 2026-10-05 §4.2).

D11: an upsert keeps indexed_at when size and mtime are unchanged. The wave: the distinct,
non-empty normalized artist names of files indexed or gone missing strictly after a time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from backend.domain.enums import EnrichmentStatus, FileStatus
from backend.domain.library import AudioMetadata, LibraryFile
from tests.fakes.library_files import FakeLibraryFileRepository

LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)
W = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _file(
    path: str,
    *,
    mtime: int = 1,
    name: str | None = "abba",
    indexed_at: datetime = LONG_AGO,
    missing_since: datetime | None = None,
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=path,
        format="flac",
        enrichment_status=EnrichmentStatus.PENDING,
        file_status=FileStatus.MISSING if missing_since else FileStatus.PRESENT,
        file_size=100,
        file_mtime_ns=mtime,
        indexed_at=indexed_at,
        missing_since=missing_since,
        audio=AudioMetadata(track_title="Waterloo", artist_name=name, normalized_artist_name=name),
    )


def test_an_unchanged_upsert_keeps_indexed_at() -> None:
    repo = FakeLibraryFileRepository()
    repo.upsert(_file("/music/a.flac"))

    stored = repo.upsert(_file("/music/a.flac", indexed_at=datetime.now(UTC)))

    assert stored.indexed_at == LONG_AGO


def test_a_changed_upsert_takes_the_new_indexed_at() -> None:
    repo = FakeLibraryFileRepository()
    repo.upsert(_file("/music/a.flac"))
    now = datetime.now(UTC)

    stored = repo.upsert(_file("/music/a.flac", mtime=2, indexed_at=now))

    assert stored.indexed_at == now


def test_names_changed_since_holds_files_indexed_or_gone_missing_after() -> None:
    repo = FakeLibraryFileRepository()
    later = W + timedelta(minutes=1)
    repo.upsert(_file(f"/music/{uuid4().hex}.flac", name="abba", indexed_at=later))
    repo.upsert(_file(f"/music/{uuid4().hex}.flac", name="abba", indexed_at=later))
    repo.upsert(_file(f"/music/{uuid4().hex}.flac", name="queen", missing_since=later))
    repo.upsert(
        _file(f"/music/{uuid4().hex}.flac", name="blondie", indexed_at=W - timedelta(minutes=1))
    )
    repo.upsert(_file(f"/music/{uuid4().hex}.flac", name="kiss", indexed_at=W))
    repo.upsert(_file(f"/music/{uuid4().hex}.flac", name=None, indexed_at=later))
    repo.upsert(_file(f"/music/{uuid4().hex}.flac", name="", indexed_at=later))

    assert repo.normalized_artist_names_changed_since(W) == {"abba", "queen"}

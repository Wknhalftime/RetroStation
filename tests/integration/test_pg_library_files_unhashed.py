"""PG repository behaviour for rows whose content hash a first scan deferred."""
from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.domain.enums import EnrichmentStatus
from backend.domain.library import LibraryFile


def _unhashed(path: str, *, size: int = 100, mtime_ns: int = 1_000) -> LibraryFile:
    return LibraryFile(
        id=uuid4(), file_path=path, file_hash=None, format="flac",
        file_size=size, file_mtime_ns=mtime_ns,
    )


def test_unhashed_row_round_trips(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        repo.upsert(_unhashed("/music/a.flac"))
        got = repo.get_by_path("/music/a.flac")

    assert got is not None
    assert got.file_hash is None
    assert (got.file_size, got.file_mtime_ns) == (100, 1_000)


@pytest.mark.parametrize(
    ("new_size", "new_mtime_ns", "expected"),
    [
        (100, 1_000, EnrichmentStatus.ENRICHED),
        (101, 1_000, EnrichmentStatus.PENDING),
        (100, 2_000, EnrichmentStatus.PENDING),
    ],
)
def test_upsert_over_unhashed_row_keeps_enrichment_only_if_stat_unchanged(
    migrated_db: str,
    new_size: int,
    new_mtime_ns: int,
    expected: EnrichmentStatus,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        stored = _unhashed("/music/a.flac", size=100, mtime_ns=1_000)
        repo.upsert(stored)
        repo.update_recording_link(stored.id, None, EnrichmentStatus.ENRICHED)
        fresh = LibraryFile(
            id=uuid4(),
            file_path="/music/a.flac",
            file_hash="h" * 64,
            format="flac",
            enrichment_status=EnrichmentStatus.PENDING,
            file_size=new_size,
            file_mtime_ns=new_mtime_ns,
        )
        got = repo.upsert(fresh)

    assert got.enrichment_status == expected
    assert got.file_hash == "h" * 64


def test_get_unhashed_by_stat_matches_only_unhashed_rows_with_equal_stat(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        repo.upsert(_unhashed("/m/a.flac", size=10, mtime_ns=5))
        repo.upsert(_unhashed("/m/b.flac", size=10, mtime_ns=6))
        missing = _unhashed("/m/c.flac", size=10, mtime_ns=5)
        repo.upsert(missing)
        repo.mark_missing(missing.file_path)
        hashed = _unhashed("/m/d.flac", size=10, mtime_ns=5)
        hashed.file_hash = "h" * 64
        repo.upsert(hashed)

        got = [f.file_path for f in repo.get_unhashed_by_stat(10, 5)]

    assert got == ["/m/a.flac", "/m/c.flac"]


def test_get_unhashed_after_pages_present_unhashed_rows_in_path_order(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        for name in ("c", "a", "b", "d"):
            repo.upsert(_unhashed(f"/m/{name}.flac"))
        repo.mark_missing("/m/d.flac")
        hashed = _unhashed("/m/e.flac")
        hashed.file_hash = "h" * 64
        repo.upsert(hashed)

        first = [f.file_path for f in repo.get_unhashed_after(None, 2)]
        rest = [f.file_path for f in repo.get_unhashed_after("/m/b.flac", 2)]
        count = repo.count_unhashed()

    assert first == ["/m/a.flac", "/m/b.flac"]
    assert rest == ["/m/c.flac"]
    assert count == 3


def test_set_file_hash_writes_only_over_an_unchanged_unhashed_row(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        row = _unhashed("/m/a.flac", size=100, mtime_ns=1_000)
        repo.upsert(row)

        stale_stat = repo.set_file_hash(row.id, "x" * 64, 100, 2_000)
        written = repo.set_file_hash(row.id, "h" * 64, 100, 1_000)
        again = repo.set_file_hash(row.id, "y" * 64, 100, 1_000)
        got = repo.get_by_path("/m/a.flac")

    assert (stale_stat, written, again) == (False, True, False)
    assert got is not None
    assert got.file_hash == "h" * 64

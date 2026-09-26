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

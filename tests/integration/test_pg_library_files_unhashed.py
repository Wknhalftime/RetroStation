"""PG repository behaviour for rows whose content hash a first scan deferred."""
from __future__ import annotations

from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.library_files import PgLibraryFileRepository
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

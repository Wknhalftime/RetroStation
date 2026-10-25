"""PG: a library file row the database refuses is a domain error, and it rolls back alone."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.library_quarantine import PgLibraryQuarantineRepository
from backend.domain.library import LibraryFile, LibraryFileRejectedError
from backend.services.library_scan_service import scan_folder_incrementally


def _row(path: str) -> LibraryFile:
    return LibraryFile(id=uuid4(), file_path=path, format="mp3", file_size=100, file_mtime_ns=1)


def _nul_in_a_text_field(path: str) -> LibraryFile:
    """psycopg refuses it before sending: the transaction is untouched."""
    row = _row(path)
    row.audio.artist_mbid = "mbid-1\x00mbid-2"
    return row


def _nul_in_raw_metadata(path: str) -> LibraryFile:
    """The server refuses it (jsonb holds no \\u0000), which aborts the transaction."""
    row = _row(path)
    row.audio.raw_metadata = {"TXXX:MusicBrainz Artist Id": "mbid-1\x00mbid-2"}
    return row


@pytest.mark.parametrize("refused_row", [_nul_in_a_text_field, _nul_in_raw_metadata])
def test_a_refused_row_rolls_back_alone(
    migrated_db: str,
    refused_row: Callable[[str], LibraryFile],
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        repo.upsert(_row("/m/1-before.mp3"))
        with pytest.raises(LibraryFileRejectedError, match="NUL|0000"):
            repo.upsert(refused_row("/m/2-refused.mp3"))
        repo.upsert(_row("/m/3-after.mp3"))
        conn.commit()

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        names = ("1-before", "2-refused", "3-after")
        stored = {n: repo.get_by_path(f"/m/{n}.mp3") is not None for n in names}
    assert stored == {"1-before": True, "2-refused": False, "3-after": True}


@pytest.mark.parametrize("refused_row", [_nul_in_a_text_field, _nul_in_raw_metadata])
def test_a_refused_first_row_leaves_the_connection_usable(
    migrated_db: str,
    refused_row: Callable[[str], LibraryFile],
) -> None:
    """No transaction open yet: no savepoint to roll back to, and nothing earlier to keep."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        with pytest.raises(LibraryFileRejectedError):
            repo.upsert(refused_row("/m/1-refused.mp3"))
        repo.upsert(_row("/m/2-after.mp3"))
        conn.commit()

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        assert repo.get_by_path("/m/1-refused.mp3") is None
        assert repo.get_by_path("/m/2-after.mp3") is not None


@pytest.mark.parametrize("refused_row", [_nul_in_a_text_field, _nul_in_raw_metadata])
def test_a_folder_visit_writes_its_other_files_around_a_refused_row(
    migrated_db: str,
    tmp_path: Path,
    refused_row: Callable[[str], LibraryFile],
) -> None:
    """The watcher's path end to end: the visit, then the commit library_scan_files_task makes."""
    for name in ("1-before.mp3", "2-refused.mp3", "3-after.mp3"):
        (tmp_path / name).write_bytes(b"\x00" * 100)
    refused = tmp_path / "2-refused.mp3"

    def read(path: Path) -> LibraryFile:
        return refused_row(str(path)) if path == refused else _row(str(path))

    with (
        psycopg.connect(migrated_db, row_factory=dict_row) as conn,
        patch("backend.services.library_scan_service.read_tags", side_effect=read),
    ):
        result = scan_folder_incrementally(
            folder_path=tmp_path,
            file_repo=PgLibraryFileRepository(conn),
            quarantine_repo=PgLibraryQuarantineRepository(conn),
        )
        conn.commit()

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        files = PgLibraryFileRepository(conn)
        written = {p.name for p in tmp_path.iterdir() if files.get_by_path(str(p)) is not None}
        quarantined = PgLibraryQuarantineRepository(conn).get_by_path(str(refused))
    assert written == {"1-before.mp3", "3-after.mp3"}
    assert result.failing_paths == {str(refused)}
    assert quarantined is not None

"""Integration (spec C1): each scan path that marks a row MISSING stamps missing_since,
and each path that brings it back clears it (PR B's restore, move adoption, the upsert)."""

from __future__ import annotations

import shutil
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from backend.domain.enums import FileStatus
from backend.domain.library import LibraryFile
from backend.services.library_scan_service import scan_folder_incrementally
from backend.services.repository_factory import RepositoryFactory
from tests.integration.missing_file_seed import Conn, full_scan, put, retag


def _visit(conn: Conn, folder: Path) -> None:
    """One watcher-style visit of *folder*, committed."""
    repos = RepositoryFactory(conn)
    scan_folder_incrementally(
        folder_path=folder,
        file_repo=repos.library_files,
        quarantine_repo=repos.library_quarantine,
    )
    conn.commit()


def _row(conn: Conn, path: Path) -> LibraryFile:
    row = RepositoryFactory(conn).library_files.get_by_path(str(path))
    assert row is not None
    return row


def test_a_folder_visit_stamps_a_file_gone_from_its_folder(
    migrated_db: str, tmp_path: Path
) -> None:
    track = put("well_tagged.mp3", tmp_path / "album" / "a.mp3")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        _visit(conn, track.parent)
        track.unlink()
        _visit(conn, track.parent)
        gone = _row(conn, track)

    assert gone.file_status == FileStatus.MISSING
    assert gone.missing_since is not None


def test_a_full_scan_stamps_a_file_gone_from_the_library(migrated_db: str, tmp_path: Path) -> None:
    kept = put("well_tagged.mp3", tmp_path / "album" / "a.mp3")
    gone = put("partial_tags.mp3", tmp_path / "album" / "b.mp3")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        full_scan(conn, tmp_path)
        gone.unlink()
        full_scan(conn, tmp_path)
        rows = [_row(conn, kept), _row(conn, gone)]

    assert [r.file_status for r in rows] == [FileStatus.PRESENT, FileStatus.MISSING]
    assert [r.missing_since is None for r in rows] == [True, False]


def test_a_reappeared_unchanged_file_is_missing_since_nothing(
    migrated_db: str, tmp_path: Path
) -> None:
    track = put("well_tagged.mp3", tmp_path / "album" / "a.mp3")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        _visit(conn, track.parent)
        RepositoryFactory(conn).library_files.mark_missing(str(track))
        conn.commit()
        _visit(conn, track.parent)
        back = _row(conn, track)

    assert (back.file_status, back.missing_since) == (FileStatus.PRESENT, None)


def test_a_reappeared_retagged_file_is_missing_since_nothing(
    migrated_db: str, tmp_path: Path
) -> None:
    track = put("well_tagged.mp3", tmp_path / "album" / "a.mp3")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        _visit(conn, track.parent)
        RepositoryFactory(conn).library_files.mark_missing(str(track))
        conn.commit()
        retag(track)
        _visit(conn, track.parent)
        back = _row(conn, track)

    assert (back.file_status, back.missing_since) == (FileStatus.PRESENT, None)


def test_a_full_scan_clears_a_file_back_on_disk(migrated_db: str, tmp_path: Path) -> None:
    root, away = tmp_path / "lib", tmp_path / "away"
    put("well_tagged.mp3", root / "album" / "a.mp3")
    track = put("partial_tags.mp3", root / "album" / "b.mp3")
    away.mkdir()
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        full_scan(conn, root)
        row_id = _row(conn, track).id
        shutil.move(track, away / track.name)
        full_scan(conn, root)
        assert _row(conn, track).missing_since is not None
        shutil.move(away / track.name, track)
        full_scan(conn, root)
        back = _row(conn, track)

    assert (back.id, back.file_status, back.missing_since) == (row_id, FileStatus.PRESENT, None)


def test_a_full_scan_move_onto_a_missing_row_clears_it(migrated_db: str, tmp_path: Path) -> None:
    root, away = tmp_path / "lib", tmp_path / "away"
    put("well_tagged.mp3", root / "album" / "a.mp3")
    old = put("partial_tags.mp3", root / "old" / "b.mp3")
    new = root / "new" / "b.mp3"
    away.mkdir()
    new.parent.mkdir()
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        full_scan(conn, root)
        row_id = _row(conn, old).id
        shutil.move(old, away / old.name)
        full_scan(conn, root)
        assert _row(conn, old).missing_since is not None
        shutil.move(away / old.name, new)  # a rename keeps size and mtime
        full_scan(conn, root)
        moved = _row(conn, new)

    assert (moved.id, moved.file_status, moved.missing_since) == (row_id, FileStatus.PRESENT, None)


def test_a_folder_visit_move_onto_a_missing_row_clears_it(migrated_db: str, tmp_path: Path) -> None:
    old = put("well_tagged.mp3", tmp_path / "old" / "a.mp3")
    new = tmp_path / "new" / "a.mp3"
    new.parent.mkdir()
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        _visit(conn, old.parent)
        row_id = _row(conn, old).id
        shutil.move(old, new)
        _visit(conn, old.parent)
        assert _row(conn, old).missing_since is not None
        _visit(conn, new.parent)
        moved = _row(conn, new)

    assert (moved.id, moved.file_status, moved.missing_since) == (row_id, FileStatus.PRESENT, None)

"""Integration (spec C4): library.purge_missing = after_scan makes a full scan delete the
missing rows nothing replaces, after a successful reconciliation, never on an empty walk, and
never from the watcher's reconciliation."""

from __future__ import annotations

import shutil
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.domain.enums import FileStatus, MatchStatus
from backend.domain.library import PURGE_MISSING_SETTING
from backend.domain.system import UserSetting
from backend.services.library_scan_service import scan_folder_incrementally
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.library_scan_tasks import reconcile_missing_after_scan
from tests.integration.missing_file_seed import Conn, full_scan, identity, match, put


def _purge_after_scan(conn: Conn) -> None:
    RepositoryFactory(conn).user_settings.upsert(
        UserSetting(key=PURGE_MISSING_SETTING, value="after_scan")
    )
    conn.commit()


def _visit(conn: Conn, folder: Path) -> None:
    """One watcher-style visit of *folder*, committed."""
    repos = RepositoryFactory(conn)
    scan_folder_incrementally(
        folder_path=folder,
        file_repo=repos.library_files,
        quarantine_repo=repos.library_quarantine,
    )
    conn.commit()


def _refuse(*_args: object) -> None:
    raise psycopg.errors.SerializationFailure("simulated")


def test_after_scan_deletes_a_missing_row_nothing_replaces(
    migrated_db: str, tmp_path: Path
) -> None:
    kept = put("well_tagged.mp3", tmp_path / "album" / "a.mp3")
    gone = put("partial_tags.mp3", tmp_path / "album" / "b.mp3")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        full_scan(conn, tmp_path)
        gone_row = repos.library_files.get_by_path(str(gone))
        assert gone_row is not None
        matched = identity(repos)
        match(conn, matched, gone_row.id)
        _purge_after_scan(conn)

        gone.unlink()
        full_scan(conn, tmp_path)

        after = repos.broadcast_identities.get_by_id(matched)
        rows = (
            repos.library_files.get_by_path(str(gone)),
            repos.library_files.get_by_path(str(kept)),
        )

    assert rows[0] is None and rows[1] is not None
    assert after is not None and after.match_status == MatchStatus.NEEDS_REVIEW


def test_by_default_missing_rows_stay(migrated_db: str, tmp_path: Path) -> None:
    put("well_tagged.mp3", tmp_path / "album" / "a.mp3")
    gone = put("partial_tags.mp3", tmp_path / "album" / "b.mp3")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        full_scan(conn, tmp_path)
        gone.unlink()
        full_scan(conn, tmp_path)
        row = RepositoryFactory(conn).library_files.get_by_path(str(gone))

    assert row is not None and row.file_status == FileStatus.MISSING


def test_an_empty_walk_purges_nothing(migrated_db: str, tmp_path: Path) -> None:
    root = tmp_path / "music"
    track = put("well_tagged.mp3", root / "album" / "a.mp3")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        full_scan(conn, root)
        repos.library_files.mark_missing(str(track))
        conn.commit()
        _purge_after_scan(conn)

        shutil.rmtree(root / "album")  # the drive "is not mounted": the walk sees nothing
        full_scan(conn, root)
        row = repos.library_files.get_by_path(str(track))

    assert row is not None


def test_a_failed_reconciliation_skips_the_purge(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.tasks.library_scan_tasks as scan_tasks

    put("well_tagged.mp3", tmp_path / "album" / "a.mp3")
    gone = put("partial_tags.mp3", tmp_path / "album" / "b.mp3")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        full_scan(conn, tmp_path)
        _purge_after_scan(conn)
        gone.unlink()
        monkeypatch.setattr(scan_tasks, "plan_for_library", _refuse)

        full_scan(conn, tmp_path)
        row = RepositoryFactory(conn).library_files.get_by_path(str(gone))

    assert row is not None and row.file_status == FileStatus.MISSING


def test_a_failed_purge_keeps_the_scan(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = put("well_tagged.mp3", tmp_path / "album" / "a.mp3")
    gone = put("partial_tags.mp3", tmp_path / "album" / "b.mp3")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        full_scan(conn, tmp_path)
        gone_row = repos.library_files.get_by_path(str(gone))
        assert gone_row is not None
        matched = identity(repos)
        match(conn, matched, gone_row.id)
        _purge_after_scan(conn)
        gone.unlink()
        # The database refuses the purge's delete, wherever the purge is called from.
        monkeypatch.setattr(PgLibraryFileRepository, "delete_missing", _refuse)

        full_scan(conn, tmp_path)
        rows = (
            repos.library_files.get_by_path(str(kept)),
            repos.library_files.get_by_path(str(gone)),
        )
        after = repos.broadcast_identities.get_by_id(matched)

    assert rows[0] is not None and rows[0].file_status == FileStatus.PRESENT
    assert rows[1] is not None and rows[1].file_status == FileStatus.MISSING  # the scan's mark
    assert after is not None and after.match_status == MatchStatus.AUTO_MATCHED  # rolled back


def test_the_watchers_reconciliation_purges_nothing(migrated_db: str, tmp_path: Path) -> None:
    put("well_tagged.mp3", tmp_path / "album" / "a.mp3")
    gone = put("partial_tags.mp3", tmp_path / "album" / "b.mp3")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _purge_after_scan(conn)
        _visit(conn, gone.parent)
        gone.unlink()
        _visit(conn, gone.parent)

        reconcile_missing_after_scan(conn, repos)  # what the watcher runs after its visits
        row = repos.library_files.get_by_path(str(gone))

    assert row is not None and row.file_status == FileStatus.MISSING

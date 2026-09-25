"""Integration: a quarantine entry lasts only as long as its file keeps failing.

Before this, entries were never removed. A file quarantined while half
copied, then fixed or deleted, stayed in the quarantine count forever,
and the full scan added a fresh duplicate entry on every run.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.domain.library import LibraryQuarantine
from backend.services.library_scan_service import scan_folder_incrementally
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.library_scan_tasks import _run_scan

pytestmark = pytest.mark.integration

_AUDIO = Path(__file__).parent.parent / "fixtures" / "audio"


def _put(src_name: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_AUDIO / src_name, dest)
    return dest


def _visit(repos: RepositoryFactory, folder: Path) -> None:
    scan_folder_incrementally(
        folder_path=folder,
        file_repo=repos.library_files,
        quarantine_repo=repos.library_quarantine,
    )


def _full_scan(conn: psycopg.Connection[dict[str, object]], root: Path) -> None:
    _run_scan(
        root_path=str(root),
        library_conn=conn,
        repos=RepositoryFactory(conn),
        progress_repo=PgTaskProgressRepository(conn),
        task_id=uuid4().hex,
    )
    conn.commit()


def _entries(repos: RepositoryFactory, path: Path) -> int:
    return sum(1 for q in repos.library_quarantine.list_all() if q.file_path == str(path))


# --- incremental (watcher) folder visit -------------------------------------


def test_visit_clears_entry_for_a_file_that_now_reads(
    migrated_db: str, tmp_path: Path,
) -> None:
    track = _put("corrupt.mp3", tmp_path / "album" / "a.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _visit(repos, track.parent)
        assert _entries(repos, track) == 1

        _put("well_tagged.mp3", track)  # the copy finished
        _visit(repos, track.parent)

        assert _entries(repos, track) == 0
        assert repos.library_files.get_by_path(str(track)) is not None


def test_visit_clears_entry_for_a_deleted_file(migrated_db: str, tmp_path: Path) -> None:
    track = _put("corrupt.mp3", tmp_path / "album" / "a.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _visit(repos, track.parent)
        track.unlink()
        _visit(repos, track.parent)

        assert _entries(repos, track) == 0


def test_visit_keeps_entry_for_a_file_still_failing(migrated_db: str, tmp_path: Path) -> None:
    track = _put("corrupt.mp3", tmp_path / "album" / "a.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _visit(repos, track.parent)
        _visit(repos, track.parent)

        assert _entries(repos, track) == 1


def test_visit_leaves_subfolder_entries_alone(migrated_db: str, tmp_path: Path) -> None:
    """The folder visit is non-recursive; a child folder's entries are not its to judge."""
    album = tmp_path / "album"
    nested = _put("corrupt.mp3", album / "disc2" / "a.mp3")
    _put("well_tagged.mp3", album / "b.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _visit(repos, nested.parent)
        _visit(repos, album)

        assert _entries(repos, nested) == 1


def test_unreadable_folder_keeps_its_entries(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = _put("corrupt.mp3", tmp_path / "album" / "a.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _visit(repos, track.parent)

        real_iterdir = Path.iterdir

        def _denied(self: Path) -> object:
            if self == track.parent:
                raise PermissionError(13, "Access is denied", str(self))
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", _denied)
        _visit(repos, track.parent)

        assert _entries(repos, track) == 1


def test_visit_clears_historical_duplicates(migrated_db: str, tmp_path: Path) -> None:
    """Scans before this recorded one entry per failed visit."""
    track = _put("well_tagged.mp3", tmp_path / "album" / "a.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        for _ in range(3):
            repos.library_quarantine.create_write_only(
                LibraryQuarantine(id=uuid4(), file_path=str(track), error_message="old"),
            )
        _visit(repos, track.parent)

        assert _entries(repos, track) == 0


# --- full "Scan Library" scan ------------------------------------------------


def test_full_scan_records_a_failing_file_once(migrated_db: str, tmp_path: Path) -> None:
    track = _put("corrupt.mp3", tmp_path / "album" / "a.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _full_scan(conn, tmp_path)
        _full_scan(conn, tmp_path)

        assert _entries(repos, track) == 1


def test_full_scan_clears_fixed_and_deleted_files(migrated_db: str, tmp_path: Path) -> None:
    fixed = _put("corrupt.mp3", tmp_path / "album" / "fixed.mp3")
    deleted = _put("corrupt.mp3", tmp_path / "album" / "deleted.mp3")
    _put("well_tagged.mp3", tmp_path / "album" / "ok.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _full_scan(conn, tmp_path)
        _put("well_tagged.mp3", fixed)
        deleted.unlink()
        _full_scan(conn, tmp_path)

        assert _entries(repos, fixed) == 0
        assert _entries(repos, deleted) == 0


def test_full_scan_of_unmounted_root_keeps_entries(migrated_db: str, tmp_path: Path) -> None:
    root = tmp_path / "music"
    track = _put("corrupt.mp3", root / "album" / "a.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _full_scan(conn, root)
        shutil.rmtree(root)
        _full_scan(conn, root)

        assert _entries(repos, track) == 1


def test_full_scan_leaves_entries_outside_root(migrated_db: str, tmp_path: Path) -> None:
    root = tmp_path / "Music"
    _put("well_tagged.mp3", root / "a.mp3")
    outside = str(tmp_path / "Music2" / "b.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        repos.library_quarantine.create_write_only(
            LibraryQuarantine(id=uuid4(), file_path=outside, error_message="elsewhere"),
        )
        _full_scan(conn, root)

        assert sum(1 for q in repos.library_quarantine.list_all() if q.file_path == outside) == 1

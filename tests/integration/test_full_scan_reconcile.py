"""Integration: the full "Scan Library" scan reconciles the DB with the disk.

Before this, the full scan only ever upserted what it found. A deleted
file stayed PRESENT forever, and a moved or renamed file was inserted as a
bare new row while its work/recording links stayed on the old one.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.domain.enums import FileStatus
from backend.domain.library import LibraryFile
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.library_scan_tasks import _run_scan

pytestmark = pytest.mark.integration

_AUDIO = Path(__file__).parent.parent / "fixtures" / "audio"


def _put(src_name: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_AUDIO / src_name, dest)
    return dest


def _full_scan(conn: psycopg.Connection[dict[str, object]], root: Path) -> None:
    _run_scan(
        root_path=str(root),
        library_conn=conn,
        repos=RepositoryFactory(conn),
        progress_repo=PgTaskProgressRepository(conn),
        task_id=uuid4().hex,
    )
    conn.commit()


def _link_to_work(repos: RepositoryFactory, file_path: Path) -> str:
    """Give an indexed file a work link of its own (not the grouping pass's)."""
    artist_id = repos.artists.upsert_local_artist("Prince", "prince")
    work_id = repos.works.create_local("Kiss (linked by hand)", artist_id)
    repos.library_files.update_work_id(_id_of(repos, file_path), work_id)
    return work_id


def _id_of(repos: RepositoryFactory, file_path: Path) -> UUID:
    lf = repos.library_files.get_by_path(str(file_path))
    assert lf is not None
    return lf.id


def _status(repos: RepositoryFactory, path: Path) -> FileStatus | None:
    lf = repos.library_files.get_by_path(str(path))
    return lf.file_status if lf is not None else None


def test_deleted_file_is_marked_missing(migrated_db: str, tmp_path: Path) -> None:
    kept = _put("well_tagged.mp3", tmp_path / "album" / "a.mp3")
    gone = _put("partial_tags.mp3", tmp_path / "album" / "b.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _full_scan(conn, tmp_path)
        gone.unlink()
        _full_scan(conn, tmp_path)

        assert _status(repos, kept) == FileStatus.PRESENT
        assert _status(repos, gone) == FileStatus.MISSING


def test_moved_file_keeps_links(migrated_db: str, tmp_path: Path) -> None:
    old = _put("well_tagged.mp3", tmp_path / "unsorted" / "kiss.mp3")
    new = tmp_path / "Prince" / "kiss.mp3"

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _full_scan(conn, tmp_path)
        work_id = _link_to_work(repos, old)
        row_id = _id_of(repos, old)
        conn.commit()

        new.parent.mkdir()
        old.rename(new)
        _full_scan(conn, tmp_path)

        moved = repos.library_files.get_by_path(str(new))
        assert moved is not None
        assert moved.id == row_id
        assert moved.work_id == work_id
        assert moved.file_status == FileStatus.PRESENT
        assert repos.library_files.get_by_path(str(old)) is None


def test_case_only_rename_keeps_links(migrated_db: str, tmp_path: Path) -> None:
    track = _put("well_tagged.mp3", tmp_path / "album" / "Track.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _full_scan(conn, tmp_path)
        work_id = _link_to_work(repos, track)
        row_id = _id_of(repos, track)
        conn.commit()

        track.rename(track.with_name("track.mp3"))
        _full_scan(conn, tmp_path)

        renamed = repos.library_files.get_by_path(str(track.with_name("track.mp3")))
        assert renamed is not None
        assert renamed.id == row_id
        assert renamed.work_id == work_id
        assert repos.library_files.get_by_path(str(track)) is None


def test_case_only_rename_with_new_tags_keeps_the_row(
    migrated_db: str, tmp_path: Path,
) -> None:
    """A tagger's rename also rewrites the tags, so the content no longer
    matches; the old spelling naming the same file is what identifies it."""
    probe = tmp_path / "CaseProbe"
    probe.touch()
    if not (tmp_path / "caseprobe").exists():
        pytest.skip("case-only renames are real moves on a case-sensitive filesystem")
    probe.unlink()
    track = _put("well_tagged.mp3", tmp_path / "album" / "Track.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _full_scan(conn, tmp_path)
        work_id = _link_to_work(repos, track)
        row_id = _id_of(repos, track)
        conn.commit()

        renamed = track.with_name("track.mp3")
        track.rename(renamed)
        with renamed.open("ab") as f:
            f.write(b"\0" * 256)
        _full_scan(conn, tmp_path)

        assert repos.library_files.get_path_statuses_under(str(tmp_path)) == {
            str(renamed): FileStatus.PRESENT,
        }
        kept = repos.library_files.get_by_path(str(renamed))
        assert kept is not None
        assert kept.id == row_id
        assert kept.work_id == work_id


def test_duplicate_copy_stays_a_separate_row(migrated_db: str, tmp_path: Path) -> None:
    original = _put("well_tagged.mp3", tmp_path / "album" / "kiss.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _full_scan(conn, tmp_path)
        work_id = _link_to_work(repos, original)
        conn.commit()

        copy = _put("well_tagged.mp3", tmp_path / "best-of" / "kiss.mp3")
        _full_scan(conn, tmp_path)

        kept = repos.library_files.get_by_path(str(original))
        added = repos.library_files.get_by_path(str(copy))
        assert kept is not None and added is not None
        assert kept.id != added.id
        assert kept.work_id == work_id
        assert kept.file_status == added.file_status == FileStatus.PRESENT


def test_unmounted_root_marks_nothing_missing(migrated_db: str, tmp_path: Path) -> None:
    """A drive that is not plugged in is not a library that was deleted."""
    root = tmp_path / "music"
    track = _put("well_tagged.mp3", root / "album" / "a.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _full_scan(conn, root)
        shutil.rmtree(root)
        _full_scan(conn, root)

        assert _status(repos, track) == FileStatus.PRESENT


def test_empty_root_marks_nothing_missing(migrated_db: str, tmp_path: Path) -> None:
    """An empty mount-point folder usually means the drive behind it is absent."""
    root = tmp_path / "music"
    track = _put("well_tagged.mp3", root / "album" / "a.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _full_scan(conn, root)
        shutil.rmtree(root / "album")
        _full_scan(conn, root)

        assert _status(repos, track) == FileStatus.PRESENT


def test_files_outside_root_are_untouched(migrated_db: str, tmp_path: Path) -> None:
    """Rows under a sibling whose name extends the root's are not the root's."""
    root = tmp_path / "Music"
    _put("well_tagged.mp3", root / "a.mp3")
    sibling = str(tmp_path / "Music2" / "b.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        repos.library_files.upsert(LibraryFile(
            id=uuid4(), file_path=sibling, file_hash="elsewhere", format="mp3",
        ))
        conn.commit()
        _full_scan(conn, root)

        row = repos.library_files.get_by_path(sibling)
        assert row is not None
        assert row.file_status == FileStatus.PRESENT

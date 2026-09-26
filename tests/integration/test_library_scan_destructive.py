"""Destructive probes: what the incremental scan does when the disk misbehaves.

Each test puts a real library (fixture audio copied under ``tmp_path``)
through something a user's disk actually does — a crashed worker, a
folder copied from a Mac, a deleted album, an unreadable folder, a rename
— and asserts the scan's outcome against the real PostgreSQL repositories.
"""
from __future__ import annotations

import os
import shutil
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.enums import FileStatus
from backend.services.folder_hash_service import diff_tree
from backend.services.library_scan_service import scan_folder_incrementally
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.library_watcher_tasks import detect_changed_folders

pytestmark = pytest.mark.integration

_AUDIO = Path(__file__).parent.parent / "fixtures" / "audio"


def _put(src_name: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_AUDIO / src_name, dest)
    return dest


def _scan(repos: RepositoryFactory, folder: Path | str) -> None:
    scan_folder_incrementally(
        folder_path=Path(folder),
        file_repo=repos.library_files,
        quarantine_repo=repos.library_quarantine,
    )


def _link_to_work(repos: RepositoryFactory, file_path: Path) -> str:
    """Give an indexed file the work link the grouping pass would build."""
    artist_id = repos.artists.upsert_local_artist("Prince", "prince")
    work_id = repos.works.create_local("Kiss", artist_id)
    lf = repos.library_files.get_by_path(str(file_path))
    assert lf is not None
    repos.library_files.update_work_id(lf.id, work_id)
    return work_id


def _changed_after_staging(
    migrated_db: str, tmp_path: Path, staged_age: timedelta,
) -> list[str]:
    """Stage the album's hash *staged_age* ago, change the album, then poll."""
    album = tmp_path / "album"
    _put("well_tagged.mp3", album / "a.mp3")
    now = datetime.now(UTC)

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        diff_tree(str(tmp_path), repos.library_folders)  # baseline
        folder = repos.library_folders.get_by_path(str(album))
        assert folder is not None
        repos.library_folders.stage_hashes([(folder.id, "x")], "scan-task")
        conn.execute(
            "UPDATE library_folder_staged_hashes SET staged_at = %s",
            (now - staged_age,),
        )
        conn.commit()

        _put("partial_tags.mp3", album / "b.mp3")
        changed, _ = detect_changed_folders(
            repos.library_folders, repos.library_folders, str(tmp_path), now,
        )
    return changed


def test_crashed_scan_does_not_hide_its_folders_forever(
    migrated_db: str, tmp_path: Path,
) -> None:
    """A worker killed mid-scan never clears its staged hashes. The poll
    treats staged folders as in flight, so without an expiry those folders
    are skipped by every later poll."""
    changed = _changed_after_staging(migrated_db, tmp_path, timedelta(hours=2))
    assert str(tmp_path / "album") in changed


def test_live_scan_folders_still_skipped(migrated_db: str, tmp_path: Path) -> None:
    changed = _changed_after_staging(migrated_db, tmp_path, timedelta(minutes=10))
    assert str(tmp_path / "album") not in changed


def test_folder_named_in_nfd_is_scanned(migrated_db: str, tmp_path: Path) -> None:
    """Folders copied from macOS keep decomposed (NFD) names; NTFS does not
    fold them to NFC, so a path rewritten to NFC names nothing on disk."""
    album = tmp_path / unicodedata.normalize("NFD", "Café Tacvba")
    _put("well_tagged.mp3", album / "a.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        diff_tree(str(tmp_path), repos.library_folders)  # baseline
        _scan(repos, album)
        conn.commit()

        _put("partial_tags.mp3", album / "b.mp3")
        changed, _ = diff_tree(str(tmp_path), repos.library_folders)
        for folder in changed:
            _scan(repos, folder)

        assert repos.library_files.get_by_path(str(album / "b.mp3")) is not None


def test_deleted_folder_marks_its_files_missing(migrated_db: str, tmp_path: Path) -> None:
    album = tmp_path / "album"
    track = _put("well_tagged.mp3", album / "a.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        diff_tree(str(tmp_path), repos.library_folders)  # baseline
        _scan(repos, album)
        conn.commit()

        shutil.rmtree(album)
        changed, _ = diff_tree(str(tmp_path), repos.library_folders)
        for folder in changed:
            _scan(repos, folder)

        row = repos.library_files.get_by_path(str(track))
        assert row is not None
        assert row.file_status == FileStatus.MISSING


def test_unreadable_folder_does_not_abort_the_scan(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One folder the worker cannot list must not fail the batch (the task
    re-raises, so every later poll fails on it again) nor mark its files
    missing: unreadable is not gone."""
    album = tmp_path / "album"
    track = _put("well_tagged.mp3", album / "a.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _scan(repos, album)
        conn.commit()

        real_iterdir = Path.iterdir

        def _denied(self: Path) -> object:
            if self == album:
                raise PermissionError(13, "Access is denied", str(self))
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", _denied)
        _scan(repos, album)

        row = repos.library_files.get_by_path(str(track))
        assert row is not None
        assert row.file_status == FileStatus.PRESENT


def test_case_only_rename_keeps_links(migrated_db: str, tmp_path: Path) -> None:
    """Taggers that rename files (Picard, Mp3tag) often change only case."""
    album = tmp_path / "album"
    track = _put("well_tagged.mp3", album / "Track.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _scan(repos, album)
        work_id = _link_to_work(repos, track)
        conn.commit()

        track.rename(album / "track.mp3")
        _scan(repos, album)

        present = [
            f for f in repos.library_files.get_by_folder_path(str(album))
            if f.file_status == FileStatus.PRESENT
        ]
        assert [f.file_path for f in present] == [str(album / "track.mp3")]
        assert present[0].work_id == work_id


def _skip_unless_case_insensitive(folder: Path) -> None:
    """A case-only rename is only a rename where the old spelling still resolves."""
    probe = folder / "CaseProbe"
    probe.touch()
    try:
        if not (folder / "caseprobe").exists():
            pytest.skip("case-only renames are real moves on a case-sensitive filesystem")
    finally:
        probe.unlink()


def _retag(path: Path) -> None:
    """What a tagger does when it renames: new bytes, new size, new mtime."""
    with path.open("ab") as f:
        f.write(b"\0" * 256)


def test_case_only_rename_with_new_tags_keeps_the_row(
    migrated_db: str, tmp_path: Path,
) -> None:
    """Picard and Mp3tag retag and rename in one go, so the content hash of
    the renamed file no longer matches its old row. The old spelling still
    names the same file, which is enough to know it is the same row."""
    _skip_unless_case_insensitive(tmp_path)
    album = tmp_path / "album"
    track = _put("well_tagged.mp3", album / "Track.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _scan(repos, album)
        work_id = _link_to_work(repos, track)
        row = repos.library_files.get_by_path(str(track))
        assert row is not None
        conn.commit()

        renamed = album / "track.mp3"
        track.rename(renamed)
        _retag(renamed)
        _scan(repos, album)

        rows = repos.library_files.get_by_folder_path(str(album))
        assert [(f.file_path, f.file_status) for f in rows] == [
            (str(renamed), FileStatus.PRESENT),
        ]
        assert rows[0].id == row.id
        assert rows[0].work_id == work_id


@pytest.mark.parametrize("old_first", [True, False], ids=["old-first", "new-first"])
def test_case_only_folder_rename_keeps_one_row(
    migrated_db: str, tmp_path: Path, old_first: bool,
) -> None:
    """The watcher sees the old spelling vanish and the new one appear. On a
    case-insensitive disk the vanished spelling still lists its files, so
    visiting it must not bring the old rows back as present."""
    _skip_unless_case_insensitive(tmp_path)
    old_album = tmp_path / "Keep The Faith"
    new_album = tmp_path / "Keep the Faith"
    track = _put("well_tagged.mp3", old_album / "track.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        diff_tree(str(tmp_path), repos.library_folders)  # baseline
        _scan(repos, old_album)
        work_id = _link_to_work(repos, track)
        row = repos.library_files.get_by_path(str(track))
        assert row is not None
        conn.commit()

        old_album.rename(new_album)
        changed, _ = diff_tree(str(tmp_path), repos.library_folders)
        assert {str(old_album), str(new_album)} <= set(changed)
        order = [old_album, new_album] if old_first else [new_album, old_album]
        for folder in order:
            _scan(repos, folder)

        statuses = repos.library_files.get_path_statuses_under(str(tmp_path))
        assert statuses == {str(new_album / "track.mp3"): FileStatus.PRESENT}
        kept = repos.library_files.get_by_path(str(new_album / "track.mp3"))
        assert kept is not None
        assert kept.id == row.id
        assert kept.work_id == work_id


@pytest.mark.parametrize("old_first", [True, False], ids=["old-first", "new-first"])
def test_moved_file_keeps_links(
    migrated_db: str, tmp_path: Path, old_first: bool,
) -> None:
    old = _put("well_tagged.mp3", tmp_path / "unsorted" / "kiss.mp3")
    new = tmp_path / "Prince" / "kiss.mp3"

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _scan(repos, old.parent)
        work_id = _link_to_work(repos, old)
        conn.commit()

        new.parent.mkdir()
        old.rename(new)
        order = [old.parent, new.parent] if old_first else [new.parent, old.parent]
        for folder in order:
            _scan(repos, folder)

        moved = repos.library_files.get_by_path(str(new))
        assert moved is not None
        assert moved.work_id == work_id
        assert repos.library_files.get_by_path(str(old)) is None


def test_drive_root_folder_lookup_finds_its_files(migrated_db: str) -> None:
    from uuid import uuid4

    from backend.domain.library import LibraryFile

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        repos.library_files.upsert(LibraryFile(
            id=uuid4(), file_path="X:\\song.mp3", file_hash="h", format="mp3",
        ))
        found = repos.library_files.get_by_folder_path("X:\\")

    assert [f.file_path for f in found] == ["X:\\song.mp3"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Accepted trade-off: rows indexed before migration 0025 have no stored "
        "stat, and re-hashing every one of them is the full-library read the "
        "stat shortcut exists to avoid. Closes once each row is backfilled."
    ),
)
def test_legacy_row_replaced_by_older_file_is_reread(
    migrated_db: str, tmp_path: Path,
) -> None:
    """A row with no stored stat trusts 'file older than its index row'.
    A replacement restored from an archive keeps its old mtime."""
    album = tmp_path / "album"
    track = _put("well_tagged.mp3", album / "a.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _scan(repos, album)
        conn.execute("UPDATE library_files SET file_size = NULL, file_mtime_ns = NULL")
        conn.commit()
        before = repos.library_files.get_by_path(str(track))
        assert before is not None

        _put("partial_tags.mp3", track)
        os.utime(track, (1_577_836_800, 1_577_836_800))  # 2020-01-01
        _scan(repos, album)

        after = repos.library_files.get_by_path(str(track))
        assert after is not None
        assert after.file_hash != before.file_hash


def test_repeat_visits_do_not_duplicate_quarantine(
    migrated_db: str, tmp_path: Path,
) -> None:
    album = tmp_path / "album"
    bad = _put("corrupt.mp3", album / "bad.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _scan(repos, album)
        _scan(repos, album)
        _scan(repos, album)
        rows = [q for q in repos.library_quarantine.list_all() if q.file_path == str(bad)]

    assert len(rows) == 1


def test_duplicate_copy_does_not_steal_the_originals_row(
    migrated_db: str, tmp_path: Path,
) -> None:
    """Same bytes in two places, both still on disk, are two library files."""
    original = _put("well_tagged.mp3", tmp_path / "album" / "kiss.mp3")
    copy = _put("well_tagged.mp3", tmp_path / "best-of" / "kiss.mp3")

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _scan(repos, original.parent)
        work_id = _link_to_work(repos, original)
        _scan(repos, copy.parent)

        kept = repos.library_files.get_by_path(str(original))
        added = repos.library_files.get_by_path(str(copy))
        assert kept is not None and added is not None
        assert kept.id != added.id
        assert kept.work_id == work_id
        assert kept.file_status == added.file_status == FileStatus.PRESENT

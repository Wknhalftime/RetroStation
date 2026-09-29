"""Integration: after a cross-work fold every song master points at a file of its own work.

Spec 2026-09-26-missing-file-reconciliation-design.md, A1 "Applying", step 2: the old
work "re-picks its master from its own present files. A master that now points outside
the work is replaced even if it was manual". A2: a missing file is never chosen as master.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.domain.curation import SongMaster
from backend.domain.enums import SelectionMethod
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.master_selection_service import reselect_master_from_files
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.library_scan_tasks import reconcile_missing_after_scan

pytestmark = pytest.mark.integration

MASTERS_OUTSIDE_THEIR_WORK = """
    SELECT count(*) AS n
    FROM song_masters s
    JOIN library_files f ON f.id = s.preferred_file_id
    WHERE f.work_id IS DISTINCT FROM s.work_id
"""


def _masters_outside_their_work(conn: psycopg.Connection[DictRow]) -> int:
    row = conn.execute(MASTERS_OUTSIDE_THEIR_WORK).fetchone()
    assert row is not None
    return int(row["n"])


def _file(
    repos: RepositoryFactory,
    path: Path,
    work_id: str,
    recording: str,
    *,
    missing: bool = False,
) -> LibraryFile:
    """A row the planner pairs only with rows of the same *recording* (MBID rule)."""
    lf = repos.library_files.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=str(path),
            format="flac",
            work_id=work_id,
            audio=AudioMetadata(recording_mbid=recording, release_mbid="rel-1", duration_ms=54_040),
        )
    )
    if missing:
        repos.library_files.mark_missing(str(path))
    return lf


def _master(repos: RepositoryFactory, work_id: str, file_id: UUID, method: SelectionMethod) -> None:
    repos.song_masters.upsert(
        SongMaster(id=uuid4(), work_id=work_id, preferred_file_id=file_id, selection_method=method)
    )


def _master_of(repos: RepositoryFactory, work_id: str) -> tuple[UUID, SelectionMethod] | None:
    master = repos.song_masters.get_by_work(work_id)
    return None if master is None else (master.preferred_file_id, master.selection_method)


def _two_works(repos: RepositoryFactory) -> tuple[str, str]:
    artist_id = repos.artists.upsert_local_artist("Samuel L. Jackson", "samuel l jackson")
    return (
        repos.works.create_local("Ezekiel 25 17", artist_id),
        repos.works.create_local("Ezekiel 25:17", artist_id),
    )


@pytest.mark.parametrize("old_method", [SelectionMethod.MANUAL, SelectionMethod.AUTO])
def test_cross_work_fold_repicks_the_old_works_master_from_its_own_present_file(
    migrated_db: str,
    tmp_path: Path,
    old_method: SelectionMethod,
) -> None:
    """(a) MANUAL: the bug. (b) AUTO: regression guard. Same expectations for both."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        w1, w2 = _two_works(repos)
        missing = _file(repos, tmp_path / "old" / "a.flac", w1, "rec-a", missing=True)
        kept = _file(repos, tmp_path / "old" / "b.flac", w1, "rec-b")
        successor = _file(repos, tmp_path / "new" / "a.flac", w2, "rec-a")
        _master(repos, w1, missing.id, old_method)
        _master(repos, w2, successor.id, SelectionMethod.AUTO)

        result = reconcile_missing_after_scan(conn, repos)

        assert result is not None and result.reconciled == 1
        assert repos.library_files.get_by_id(missing.id) is None
        assert _masters_outside_their_work(conn) == 0
        assert _master_of(repos, w1) == (kept.id, SelectionMethod.AUTO)
        assert _master_of(repos, w2) == (successor.id, SelectionMethod.AUTO)


def test_cross_work_fold_keeps_a_manual_master_on_the_new_works_own_file(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    """The new work's own manual pick is not disturbed by a row folding into it."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        w1, w2 = _two_works(repos)
        missing = _file(repos, tmp_path / "old" / "a.flac", w1, "rec-a", missing=True)
        kept = _file(repos, tmp_path / "old" / "b.flac", w1, "rec-b")
        _file(repos, tmp_path / "new" / "a.flac", w2, "rec-a")
        chosen = _file(repos, tmp_path / "new" / "c.flac", w2, "rec-c")
        _master(repos, w1, missing.id, SelectionMethod.MANUAL)
        _master(repos, w2, chosen.id, SelectionMethod.MANUAL)

        result = reconcile_missing_after_scan(conn, repos)

        assert result is not None and result.reconciled == 1
        assert _masters_outside_their_work(conn) == 0
        assert _master_of(repos, w1) == (kept.id, SelectionMethod.AUTO)
        assert _master_of(repos, w2) == (chosen.id, SelectionMethod.MANUAL)


def test_no_master_points_outside_its_work_after_a_mixed_reconciliation(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    """(c) Invariant over one run holding every kind of fold at once."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Samuel L. Jackson", "samuel l jackson")
        works = [repos.works.create_local(f"Work {i}", artist_id) for i in range(6)]
        manual_old, auto_old, emptied, same, target_a, target_b = works
        # Cross-work, MANUAL master, old work keeps a present file.
        m1 = _file(repos, tmp_path / "old" / "1.flac", manual_old, "rec-1", missing=True)
        left1 = _file(repos, tmp_path / "old" / "1-left.flac", manual_old, "rec-1-left")
        _file(repos, tmp_path / "new" / "1.flac", target_a, "rec-1")
        _master(repos, manual_old, m1.id, SelectionMethod.MANUAL)
        # Cross-work, AUTO master, old work keeps a present file.
        m2 = _file(repos, tmp_path / "old" / "2.flac", auto_old, "rec-2", missing=True)
        left2 = _file(repos, tmp_path / "old" / "2-left.flac", auto_old, "rec-2-left")
        _file(repos, tmp_path / "new" / "2.flac", target_b, "rec-2")
        _master(repos, auto_old, m2.id, SelectionMethod.AUTO)
        # Cross-work, MANUAL master, old work left with no file: it is deleted.
        m3 = _file(repos, tmp_path / "old" / "3.flac", emptied, "rec-3", missing=True)
        _file(repos, tmp_path / "new" / "3.flac", target_a, "rec-3")
        _master(repos, emptied, m3.id, SelectionMethod.MANUAL)
        # Same-work fold, MANUAL master: follows the track and stays manual.
        m4 = _file(repos, tmp_path / "old" / "4.flac", same, "rec-4", missing=True)
        s4 = _file(repos, tmp_path / "new" / "4.flac", same, "rec-4")
        _master(repos, same, m4.id, SelectionMethod.MANUAL)

        result = reconcile_missing_after_scan(conn, repos)

        assert result is not None and result.reconciled == 4
        assert repos.works.get_by_id(emptied) is None
        assert _master_of(repos, same) == (s4.id, SelectionMethod.MANUAL)
        assert _masters_outside_their_work(conn) == 0
        assert _master_of(repos, manual_old) == (left1.id, SelectionMethod.AUTO)
        assert _master_of(repos, auto_old) == (left2.id, SelectionMethod.AUTO)


@pytest.mark.parametrize("old_method", [SelectionMethod.MANUAL, SelectionMethod.AUTO])
def test_cross_work_fold_leaves_no_master_when_the_old_work_has_only_missing_files(
    migrated_db: str,
    tmp_path: Path,
    old_method: SelectionMethod,
) -> None:
    """(d) Ruling R2: no present file to re-pick from, so the old work has no master.

    Pointing at the successor breaks "never outside the work"; pointing at the other
    missing row breaks A2 "a missing file is never chosen as a work's master".
    """
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        w1, w2 = _two_works(repos)
        missing = _file(repos, tmp_path / "old" / "a.flac", w1, "rec-a", missing=True)
        _file(repos, tmp_path / "old" / "b.flac", w1, "rec-b", missing=True)
        successor = _file(repos, tmp_path / "new" / "a.flac", w2, "rec-a")
        _master(repos, w1, missing.id, old_method)
        _master(repos, w2, successor.id, SelectionMethod.AUTO)

        result = reconcile_missing_after_scan(conn, repos)

        assert result is not None and result.reconciled == 1
        assert repos.works.get_by_id(w1) is not None
        assert _masters_outside_their_work(conn) == 0
        assert _master_of(repos, w1) is None
        assert _master_of(repos, w2) == (successor.id, SelectionMethod.AUTO)


@pytest.mark.parametrize("incoming", [SelectionMethod.AUTO, SelectionMethod.MANUAL])
def test_pg_upsert_never_overwrites_a_manual_pick(
    migrated_db: str, tmp_path: Path, incoming: SelectionMethod
) -> None:
    """The Pg contract the fake must mirror (see tests/fakes/test_song_masters_fake.py)."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        w1, _ = _two_works(repos)
        chosen = _file(repos, tmp_path / "a.flac", w1, "rec-a")
        other = _file(repos, tmp_path / "b.flac", w1, "rec-b")
        _master(repos, w1, chosen.id, SelectionMethod.MANUAL)

        _master(repos, w1, other.id, incoming)

        assert _master_of(repos, w1) == (chosen.id, SelectionMethod.MANUAL)


def test_pg_upsert_replaces_an_auto_pick(migrated_db: str, tmp_path: Path) -> None:
    """The Pg twin of the fake's test_upsert_replaces_an_auto_pick."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        w1, _ = _two_works(repos)
        first = _file(repos, tmp_path / "a.flac", w1, "rec-a")
        second = _file(repos, tmp_path / "b.flac", w1, "rec-b")
        _master(repos, w1, first.id, SelectionMethod.AUTO)

        _master(repos, w1, second.id, SelectionMethod.MANUAL)

        assert _master_of(repos, w1) == (second.id, SelectionMethod.MANUAL)


def test_reselect_replaces_a_manual_master_on_a_missing_file(
    migrated_db: str,
    tmp_path: Path,
) -> None:
    """A2: a manual master is kept only while its file is a present file of the work."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        w1, _ = _two_works(repos)
        gone = _file(repos, tmp_path / "gone.flac", w1, "rec-a", missing=True)
        here = _file(repos, tmp_path / "here.flac", w1, "rec-b")
        _master(repos, w1, gone.id, SelectionMethod.MANUAL)

        reselect_master_from_files(w1, repos.song_masters, repos.library_files)

        assert _master_of(repos, w1) == (here.id, SelectionMethod.AUTO)

"""Folding a missing row into its successor (fakes: row, master and work effects)."""

from __future__ import annotations

from uuid import uuid4

from backend.domain.catalog import Work
from backend.domain.curation import SongMaster
from backend.domain.enums import CatalogSource, SelectionMethod
from backend.domain.library import AudioMetadata, LibraryFile, MissingFileMove
from backend.services.missing_file_reconciliation_service import (
    ReconciliationRepos,
    apply_missing_file_move,
    reconcile_missing_files,
    repick_stranded_masters,
)
from tests.fakes.format_overrides import FakeFormatOverrideRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository


def _repos() -> ReconciliationRepos:
    files, works = FakeLibraryFileRepository(), FakeWorkRepository()
    masters = FakeSongMasterRepository()
    works.set_library_file_repo(files)
    masters.set_library_file_repo(files)
    return ReconciliationRepos(
        files=files,
        matches=FakeMatchRepository(),
        works=works,
        song_masters=masters,
        format_overrides=FakeFormatOverrideRepository(),
    )


def _work(repos: ReconciliationRepos, work_id: str) -> None:
    repos.works.upsert(
        Work(
            id=work_id,
            title="Ezekiel 25:17",
            artist_id="a1",
            origin=CatalogSource.LOCAL,
            needs_enhancement=False,
        )
    )


def _file(repos: ReconciliationRepos, path: str, work_id: str, *, missing: bool) -> LibraryFile:
    lf = repos.files.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=path,
            file_hash=None,
            format="flac",
            work_id=work_id,
            audio=AudioMetadata(recording_mbid="rec-1", release_mbid="rel-1", duration_ms=54_040),
        )
    )
    if missing:
        repos.files.mark_missing(path)
    return lf


def _move(old: LibraryFile, new: LibraryFile) -> MissingFileMove:
    return MissingFileMove(old.id, old.file_path, old.work_id, new.id, new.file_path, new.work_id)


def test_same_work_move_deletes_the_missing_row_only() -> None:
    repos = _repos()
    _work(repos, "w1")
    old = _file(repos, "/m/old.flac", "w1", missing=True)
    new = _file(repos, "/m/new.flac", "w1", missing=False)

    apply_missing_file_move(_move(old, new), repos)

    assert repos.files.get_by_id(old.id) is None
    assert repos.works.get_by_id("w1") is not None


def test_cross_work_move_deletes_the_emptied_old_work() -> None:
    repos = _repos()
    _work(repos, "w1")
    _work(repos, "w2")
    old = _file(repos, "/m/old.flac", "w1", missing=True)
    new = _file(repos, "/m/new.flac", "w2", missing=False)

    apply_missing_file_move(_move(old, new), repos)

    assert repos.works.get_by_id("w1") is None
    assert repos.works.get_by_id("w2") is not None


def test_cross_work_move_repicks_the_old_works_master_from_what_is_left() -> None:
    repos = _repos()
    _work(repos, "w1")
    _work(repos, "w2")
    old = _file(repos, "/m/old.flac", "w1", missing=True)
    left = _file(repos, "/m/left.flac", "w1", missing=False)
    new = _file(repos, "/m/new.flac", "w2", missing=False)
    repos.song_masters.upsert(
        SongMaster(
            id=uuid4(),
            work_id="w1",
            preferred_file_id=old.id,
            selection_method=SelectionMethod.MANUAL,
        )
    )

    apply_missing_file_move(_move(old, new), repos)

    master = repos.song_masters.get_by_work("w1")
    assert master is not None and master.preferred_file_id == left.id


def test_a_fold_reports_that_it_happened() -> None:
    repos = _repos()
    _work(repos, "w1")
    old = _file(repos, "/m/old.flac", "w1", missing=True)
    new = _file(repos, "/m/new.flac", "w1", missing=False)

    assert apply_missing_file_move(_move(old, new), repos) is True


def test_a_missing_row_restored_since_planning_is_left_alone() -> None:
    repos = _repos()
    _work(repos, "w1")
    old = _file(repos, "/m/old.flac", "w1", missing=True)
    new = _file(repos, "/m/new.flac", "w1", missing=False)
    move = _move(old, new)
    repos.files.relocate(old.id, old.file_path)

    assert apply_missing_file_move(move, repos) is False
    assert repos.files.get_by_id(old.id) is not None


def test_a_successor_gone_missing_since_planning_is_left_alone() -> None:
    repos = _repos()
    _work(repos, "w1")
    old = _file(repos, "/m/old.flac", "w1", missing=True)
    new = _file(repos, "/m/new.flac", "w1", missing=False)
    move = _move(old, new)
    repos.files.mark_missing(new.file_path)

    assert apply_missing_file_move(move, repos) is False
    assert repos.files.get_by_id(old.id) is not None
    assert repos.files.get_by_id(new.id) is not None


def test_reconcile_counts_what_it_did_and_left() -> None:
    repos = _repos()
    _work(repos, "w1")
    _file(repos, "/m/a_old.flac", "w1", missing=True)
    _file(repos, "/m/b_new.flac", "w1", missing=False)
    lonely = repos.files.upsert(
        LibraryFile(
            id=uuid4(),
            file_path="/m/z_lonely.flac",
            file_hash=None,
            format="flac",
            work_id="w1",
            audio=AudioMetadata(recording_mbid="rec-2", duration_ms=1_000),
        )
    )
    repos.files.mark_missing(lonely.file_path)

    result = reconcile_missing_files(repos)

    assert (result.reconciled, result.ambiguous, result.unmatched) == (1, 0, 1)
    assert [f.file_path for f in repos.files.get_missing()] == ["/m/z_lonely.flac"]


def _auto_master(repos: ReconciliationRepos, work_id: str, file: LibraryFile) -> None:
    repos.song_masters.upsert(
        SongMaster(
            id=uuid4(),
            work_id=work_id,
            preferred_file_id=file.id,
            selection_method=SelectionMethod.AUTO,
        )
    )


def test_repick_moves_masters_off_missing_files_where_a_present_file_exists() -> None:
    repos = _repos()
    stranded = _file(repos, "/m/stranded_old.flac", "w1", missing=True)
    on_disk = _file(repos, "/m/stranded_new.flac", "w1", missing=False)
    _auto_master(repos, "w1", stranded)
    only_missing = _file(repos, "/m/gone.flac", "w2", missing=True)
    _auto_master(repos, "w2", only_missing)
    present = _file(repos, "/m/fine.flac", "w3", missing=False)
    _auto_master(repos, "w3", present)

    assert repick_stranded_masters(repos) == 1

    masters = {w: repos.song_masters.get_by_work(w) for w in ("w1", "w2", "w3")}
    assert {w: m.preferred_file_id for w, m in masters.items() if m is not None} == {
        "w1": on_disk.id,
        "w2": only_missing.id,
        "w3": present.id,
    }

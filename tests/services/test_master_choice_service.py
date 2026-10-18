"""Choosing a work's master file by hand, including a file its page shows from another work.

A work's page lists files by recording, so it also shows a file that sits on another work
but shares a recording with this one (Stone Sour › "Bother" shows the album copy tagged
"Corey Taylor"). Crowning that file moves it onto this work, then makes it the master.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from backend.domain.catalog import Recording, Work
from backend.domain.curation import (
    MasterFileMissingError,
    MasterFileNotOnWorkError,
    MasterWorkNotFoundError,
    SongMaster,
)
from backend.domain.enums import CatalogSource, FileStatus, SelectionMethod
from backend.domain.library import LibraryFile
from backend.services.master_choice_service import MasterChoiceRepos, choose_master
from backend.services.work_move_service import WorkMoveRepos
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.recordings import FakeRecordingRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository


def _repos() -> MasterChoiceRepos:
    files, works, masters = (
        FakeLibraryFileRepository(),
        FakeWorkRepository(),
        FakeSongMasterRepository(),
    )
    works.set_library_file_repo(files)
    masters.set_library_file_repo(files)
    move = WorkMoveRepos(
        files=files, works=works, song_masters=masters, matches=FakeMatchRepository()
    )
    return MasterChoiceRepos(move=move, recordings=FakeRecordingRepository())


def _work(repos: MasterChoiceRepos, work_id: str) -> str:
    repos.move.works.upsert(
        Work(id=work_id, title="Bother", artist_id=work_id, origin=CatalogSource.LOCAL)
    )
    return work_id


def _file(
    repos: MasterChoiceRepos,
    path: str,
    work_id: str | None,
    recording_id: str | None,
    status: FileStatus = FileStatus.PRESENT,
) -> LibraryFile:
    return repos.move.files.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=path,
            format="flac",
            work_id=work_id,
            recording_id=recording_id,
            file_status=status,
        )
    )


def _master(repos: MasterChoiceRepos, work_id: str) -> SongMaster:
    master = repos.move.song_masters.get_by_work(work_id)
    assert master is not None
    return master


def test_a_file_on_the_work_becomes_its_manual_master() -> None:
    repos = _repos()
    work = _work(repos, "stone-sour")
    promo = _file(repos, r"D:\m\promo.flac", work, "rec-1")

    chosen = choose_master(work, promo.id, repos)

    assert chosen.preferred_file_id == promo.id
    assert chosen.selection_method == SelectionMethod.MANUAL
    assert _master(repos, work).preferred_file_id == promo.id


def test_a_new_choice_replaces_a_manual_master() -> None:
    repos = _repos()
    work = _work(repos, "stone-sour")
    first = _file(repos, r"D:\m\first.flac", work, "rec-1")
    second = _file(repos, r"D:\m\second.flac", work, "rec-2")
    choose_master(work, first.id, repos)

    choose_master(work, second.id, repos)

    master = _master(repos, work)
    assert master.preferred_file_id == second.id
    assert master.selection_method == SelectionMethod.MANUAL


def test_a_file_sharing_a_recording_with_the_work_moves_onto_it_and_is_master() -> None:
    repos = _repos()
    stone_sour = _work(repos, "stone-sour")
    corey_taylor = _work(repos, "corey-taylor")
    _file(repos, r"D:\m\promo.flac", stone_sour, "rec-shared")
    album = _file(repos, r"D:\m\album.flac", corey_taylor, "rec-shared")

    chosen = choose_master(stone_sour, album.id, repos)

    moved = repos.move.files.get_by_id(album.id)
    assert moved is not None
    assert moved.work_id == stone_sour
    # It keeps its recording (its MusicBrainz link), which already shows on this work.
    assert moved.recording_id == "rec-shared"
    assert chosen.preferred_file_id == album.id
    assert _master(repos, stone_sour).selection_method == SelectionMethod.MANUAL


def test_the_work_the_file_left_is_deleted_when_nothing_is_left_on_it() -> None:
    repos = _repos()
    stone_sour = _work(repos, "stone-sour")
    corey_taylor = _work(repos, "corey-taylor")
    _file(repos, r"D:\m\promo.flac", stone_sour, "rec-shared")
    album = _file(repos, r"D:\m\album.flac", corey_taylor, "rec-shared")

    choose_master(stone_sour, album.id, repos)

    assert repos.move.works.get_by_id(corey_taylor) is None


def test_a_file_on_a_recording_the_work_owns_moves_onto_it() -> None:
    repos = _repos()
    stone_sour = _work(repos, "stone-sour")
    corey_taylor = _work(repos, "corey-taylor")
    repos.recordings.upsert(Recording(id="rec-owned", title="Bother", work_id=stone_sour))
    album = _file(repos, r"D:\m\album.flac", corey_taylor, "rec-owned")

    choose_master(stone_sour, album.id, repos)

    moved = repos.move.files.get_by_id(album.id)
    assert moved is not None
    assert moved.work_id == stone_sour
    assert _master(repos, stone_sour).preferred_file_id == album.id


def test_a_file_the_work_does_not_show_is_refused_and_nothing_changes() -> None:
    repos = _repos()
    stone_sour = _work(repos, "stone-sour")
    other = _work(repos, "other")
    _file(repos, r"D:\m\promo.flac", stone_sour, "rec-1")
    stranger = _file(repos, r"D:\m\stranger.flac", other, "rec-2")

    with pytest.raises(MasterFileNotOnWorkError):
        choose_master(stone_sour, stranger.id, repos)

    kept = repos.move.files.get_by_id(stranger.id)
    assert kept is not None
    assert kept.work_id == other
    assert repos.move.song_masters.get_by_work(stone_sour) is None


def test_a_file_with_no_recording_on_another_work_is_refused() -> None:
    repos = _repos()
    stone_sour = _work(repos, "stone-sour")
    other = _work(repos, "other")
    stranger = _file(repos, r"D:\m\stranger.flac", other, None)

    with pytest.raises(MasterFileNotOnWorkError):
        choose_master(stone_sour, stranger.id, repos)


def test_an_unknown_file_is_refused() -> None:
    repos = _repos()
    stone_sour = _work(repos, "stone-sour")

    with pytest.raises(MasterFileNotOnWorkError):
        choose_master(stone_sour, uuid4(), repos)


def test_a_missing_file_is_refused_and_not_moved() -> None:
    repos = _repos()
    stone_sour = _work(repos, "stone-sour")
    corey_taylor = _work(repos, "corey-taylor")
    _file(repos, r"D:\m\promo.flac", stone_sour, "rec-shared")
    gone = _file(repos, r"D:\m\gone.flac", corey_taylor, "rec-shared", FileStatus.MISSING)

    with pytest.raises(MasterFileMissingError):
        choose_master(stone_sour, gone.id, repos)

    kept = repos.move.files.get_by_id(gone.id)
    assert kept is not None
    assert kept.work_id == corey_taylor


def test_an_unknown_work_is_refused() -> None:
    repos = _repos()
    lone = _file(repos, r"D:\m\lone.flac", None, None)

    with pytest.raises(MasterWorkNotFoundError):
        choose_master("no-such-work", lone.id, repos)

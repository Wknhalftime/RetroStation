"""A missing file is never picked as a work's song master."""

from __future__ import annotations

from uuid import UUID, uuid4

from backend.domain.catalog import Recording
from backend.domain.curation import SongMaster
from backend.domain.enums import SelectionMethod
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.master_selection_service import (
    recalculate_song_masters,
    reselect_master_from_files,
)
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.recordings import FakeRecordingRepository
from tests.fakes.song_masters import FakeSongMasterRepository

WORK = "work-1"


def _file(
    files: FakeLibraryFileRepository,
    path: str,
    fmt: str,
    *,
    missing: bool = False,
    work_id: str = WORK,
) -> LibraryFile:
    lf = files.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=path,
            format=fmt,
            work_id=work_id,
            recording_id="rec-1",
            audio=AudioMetadata(bitrate=900 if fmt == "flac" else 320),
        )
    )
    if missing:
        files.mark_missing(path)
    return lf


def _master(masters: FakeSongMasterRepository, file_id: UUID, method: SelectionMethod) -> None:
    masters.upsert(
        SongMaster(id=uuid4(), work_id=WORK, preferred_file_id=file_id, selection_method=method)
    )


def test_reselect_skips_a_missing_file_that_would_score_higher() -> None:
    files, masters = FakeLibraryFileRepository(), FakeSongMasterRepository()
    _file(files, "/m/gone.flac", "flac", missing=True)
    mp3 = _file(files, "/m/here.mp3", "mp3")

    reselect_master_from_files(WORK, masters, files)

    master = masters.get_by_work(WORK)
    assert master is not None and master.preferred_file_id == mp3.id


def test_reselect_keeps_the_master_when_no_file_is_present() -> None:
    files, masters = FakeLibraryFileRepository(), FakeSongMasterRepository()
    gone = _file(files, "/m/gone.flac", "flac", missing=True)
    _master(masters, gone.id, SelectionMethod.AUTO)

    reselect_master_from_files(WORK, masters, files)

    master = masters.get_by_work(WORK)
    assert master is not None and master.preferred_file_id == gone.id


def test_reselect_replaces_a_manual_master_whose_file_is_missing() -> None:
    files, masters = FakeLibraryFileRepository(), FakeSongMasterRepository()
    gone = _file(files, "/m/gone.flac", "flac", missing=True)
    here = _file(files, "/m/here.mp3", "mp3")
    _master(masters, gone.id, SelectionMethod.MANUAL)

    reselect_master_from_files(WORK, masters, files)

    master = masters.get_by_work(WORK)
    assert master is not None
    assert (master.preferred_file_id, master.selection_method) == (
        here.id,
        SelectionMethod.AUTO,
    )


def test_reselect_replaces_a_manual_master_whose_file_left_the_work() -> None:
    files, masters = FakeLibraryFileRepository(), FakeSongMasterRepository()
    elsewhere = _file(files, "/m/other.flac", "flac", work_id="work-2")
    here = _file(files, "/m/here.mp3", "mp3")
    _master(masters, elsewhere.id, SelectionMethod.MANUAL)

    reselect_master_from_files(WORK, masters, files)

    master = masters.get_by_work(WORK)
    assert master is not None and master.preferred_file_id == here.id


def test_reselect_keeps_a_manual_master_on_a_present_file() -> None:
    files, masters = FakeLibraryFileRepository(), FakeSongMasterRepository()
    _file(files, "/m/better.flac", "flac")
    chosen = _file(files, "/m/chosen.mp3", "mp3")
    _master(masters, chosen.id, SelectionMethod.MANUAL)

    reselect_master_from_files(WORK, masters, files)

    master = masters.get_by_work(WORK)
    assert master is not None and master.preferred_file_id == chosen.id


def test_recalculate_skips_missing_files() -> None:
    files, masters = FakeLibraryFileRepository(), FakeSongMasterRepository()
    recordings = FakeRecordingRepository()
    recordings.upsert(Recording(id="rec-1", title="Song", work_id=WORK, duration_ms=1))
    _file(files, "/m/gone.flac", "flac", missing=True)
    mp3 = _file(files, "/m/here.mp3", "mp3")

    recalculate_song_masters([WORK], masters, recordings, files)

    master = masters.get_by_work(WORK)
    assert master is not None and master.preferred_file_id == mp3.id

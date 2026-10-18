"""FakeRecordingSearchReach answers like the Pg adapter (test_recording_search_reach_pg.py)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from backend.domain.library import AudioMetadata, LibraryFile
from backend.repositories.recording_search_reach import RecordingSearchReachRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.recording_search_reach import FakeRecordingSearchReach
from tests.recording_search_reach_cases import ARTIST, CASES, MBID, FileSpec


def _add(files: FakeLibraryFileRepository, spec: FileSpec) -> None:
    path = f"/music/{uuid4().hex}.flac"
    files.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=path,
            format="flac",
            audio=AudioMetadata(
                recording_mbid=spec.recording_mbid,
                artist_mbid=spec.artist_mbid,
                album_artist_mbid=spec.album_artist_mbid,
                normalized_artist_name=spec.normalized_artist_name,
            ),
        )
    )
    if spec.missing:
        files.mark_missing(path)


@pytest.mark.parametrize(("specs", "expected"), list(CASES.values()), ids=list(CASES))
def test_has_file_beyond_artist_mbid(specs: list[FileSpec], expected: bool) -> None:
    files = FakeLibraryFileRepository()
    for spec in specs:
        _add(files, spec)
    reach: RecordingSearchReachRepository = FakeRecordingSearchReach(files)

    assert reach.has_file_beyond_artist_mbid(ARTIST, MBID) is expected


def test_the_fake_records_each_question() -> None:
    reach = FakeRecordingSearchReach(FakeLibraryFileRepository())

    reach.has_file_beyond_artist_mbid(ARTIST, MBID)
    reach.has_file_beyond_artist_mbid("other", "mb-other")

    assert reach.queries == [(ARTIST, MBID), ("other", "mb-other")]

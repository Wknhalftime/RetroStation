from __future__ import annotations

from uuid import uuid4

from backend.domain.enums import FileStatus
from backend.domain.library import AudioMetadata, LibraryFile
from tests.fakes.library_files import FakeLibraryFileRepository


def _file(artist_name: str, recording_mbid: str | None = None) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=f"/music/{artist_name.replace(' ', '_')}-{uuid4()}.mp3",
        file_hash=f"hash-{uuid4()}",
        format="mp3",
        file_status=FileStatus.PRESENT,
        audio=AudioMetadata(
            artist_name=artist_name,
            recording_mbid=recording_mbid,
        ),
    )


def test_search_by_artist_name_case_insensitive_substring() -> None:
    repo = FakeLibraryFileRepository()
    for f in [_file("Prince"), _file("The Prince of Egypt"), _file("Madonna")]:
        repo.upsert(f)
    hits = repo.search_by_artist_name("prince", limit=10)
    assert {h.audio.artist_name for h in hits} == {"Prince", "The Prince of Egypt"}


def test_search_by_artist_name_respects_limit() -> None:
    repo = FakeLibraryFileRepository()
    for i in range(5):
        repo.upsert(_file(f"Prince {i}"))
    assert len(repo.search_by_artist_name("prince", limit=3)) == 3


def test_search_by_artist_name_no_matches_returns_empty() -> None:
    repo = FakeLibraryFileRepository()
    repo.upsert(_file("Madonna"))
    assert repo.search_by_artist_name("prince", limit=10) == []


def test_get_by_recording_mbid_hit() -> None:
    repo = FakeLibraryFileRepository()
    f = _file("Prince", recording_mbid="rec-123")
    repo.upsert(f)
    got = repo.get_by_recording_mbid("rec-123")
    assert got is not None
    assert got.audio.recording_mbid == "rec-123"


def test_get_by_recording_mbid_miss_returns_none() -> None:
    repo = FakeLibraryFileRepository()
    assert repo.get_by_recording_mbid("rec-missing") is None

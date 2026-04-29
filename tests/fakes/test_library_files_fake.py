from __future__ import annotations

from typing import Any
from uuid import uuid4

from backend.domain.enums import FileStatus
from backend.domain.library import AudioMetadata, LibraryFile
from tests.fakes.library_files import FakeLibraryFileRepository

_UNSET: Any = object()


def _file(
    artist_name: str,
    *,
    normalized_artist_name: str | None = _UNSET,
    recording_mbid: str | None = None,
) -> LibraryFile:
    norm = (
        artist_name.lower()
        if normalized_artist_name is _UNSET
        else normalized_artist_name
    )
    return LibraryFile(
        id=uuid4(),
        file_path=f"/music/{artist_name.replace(' ', '_')}-{uuid4()}.mp3",
        file_hash=f"hash-{uuid4()}",
        format="mp3",
        file_status=FileStatus.PRESENT,
        audio=AudioMetadata(
            artist_name=artist_name,
            normalized_artist_name=norm,
            recording_mbid=recording_mbid,
        ),
    )


def test_get_by_normalized_artist_name_exact_match() -> None:
    repo = FakeLibraryFileRepository()
    # Substring overlap (e.g. "the prince of egypt" contains "prince") must
    # NOT match — that is the no-cross-artist invariant.
    for f in [
        _file("Prince", normalized_artist_name="prince"),
        _file("The Prince of Egypt", normalized_artist_name="the prince of egypt"),
        _file("Madonna", normalized_artist_name="madonna"),
    ]:
        repo.upsert(f)
    hits = repo.get_by_normalized_artist_name("prince", limit=10)
    assert {h.audio.artist_name for h in hits} == {"Prince"}


def test_get_by_normalized_artist_name_respects_limit() -> None:
    repo = FakeLibraryFileRepository()
    for _ in range(5):
        repo.upsert(_file("Prince", normalized_artist_name="prince"))
    assert len(repo.get_by_normalized_artist_name("prince", limit=3)) == 3


def test_get_by_normalized_artist_name_no_matches_returns_empty() -> None:
    repo = FakeLibraryFileRepository()
    repo.upsert(_file("Madonna", normalized_artist_name="madonna"))
    assert repo.get_by_normalized_artist_name("prince", limit=10) == []


def test_get_by_normalized_artist_name_empty_input_returns_empty() -> None:
    repo = FakeLibraryFileRepository()
    repo.upsert(_file("Prince", normalized_artist_name="prince"))
    assert repo.get_by_normalized_artist_name("", limit=10) == []


def test_get_by_normalized_artist_name_skips_null_normalized() -> None:
    # A library file whose normalized_artist_name is None must never satisfy
    # an equality query — fail-closed mirrors _filter_to_artist policy.
    repo = FakeLibraryFileRepository()
    repo.upsert(_file("Prince", normalized_artist_name=None))
    assert repo.get_by_normalized_artist_name("prince", limit=10) == []


def test_get_by_recording_mbid_hit() -> None:
    repo = FakeLibraryFileRepository()
    f = _file("Prince", recording_mbid="rec-123")
    repo.upsert(f)
    got = repo.get_by_recording_mbid("rec-123")
    assert len(got) == 1
    assert got[0].audio.recording_mbid == "rec-123"


def test_get_by_recording_mbid_miss_returns_empty() -> None:
    repo = FakeLibraryFileRepository()
    assert repo.get_by_recording_mbid("rec-missing") == []


def test_get_by_recording_mbid_returns_all_duplicates() -> None:
    repo = FakeLibraryFileRepository()
    repo.upsert(_file("Prince", recording_mbid="rec-dup"))
    repo.upsert(_file("Prince", recording_mbid="rec-dup"))
    got = repo.get_by_recording_mbid("rec-dup")
    assert len(got) == 2

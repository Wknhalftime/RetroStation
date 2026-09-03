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


def test_upsert_keeps_links_when_incoming_row_has_none() -> None:
    """A fresh tag extraction carries no work/recording link. Re-upserting it
    must not erase links the grouping and enrichment passes already built —
    even when the content hash changed (a retag is not a new song)."""
    repo = FakeLibraryFileRepository()
    original = _file("Prince")
    original.work_id = "work-1"
    original.recording_id = "rec-1"
    repo.upsert(original)

    fresh = LibraryFile(
        id=uuid4(), file_path=original.file_path, file_hash="retagged", format="mp3",
    )
    repo.upsert(fresh)

    got = repo.get_by_path(original.file_path)
    assert got is not None
    assert got.work_id == "work-1"
    assert got.recording_id == "rec-1"


def test_upsert_explicit_links_replace_existing() -> None:
    repo = FakeLibraryFileRepository()
    original = _file("Prince")
    original.work_id = "work-1"
    repo.upsert(original)

    relinked = LibraryFile(
        id=uuid4(), file_path=original.file_path, file_hash=original.file_hash,
        format="mp3", work_id="work-2",
    )
    repo.upsert(relinked)

    got = repo.get_by_path(original.file_path)
    assert got is not None
    assert got.work_id == "work-2"


def test_update_file_stat_records_size_and_mtime() -> None:
    repo = FakeLibraryFileRepository()
    f = _file("Prince")
    repo.upsert(f)

    repo.update_file_stat(f.id, file_size=4096, file_mtime_ns=1_700_000_000_000_000_000)

    got = repo.get_by_id(f.id)
    assert got is not None
    assert got.file_size == 4096
    assert got.file_mtime_ns == 1_700_000_000_000_000_000

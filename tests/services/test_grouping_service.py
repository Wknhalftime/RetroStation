"""Tests for the 4-step grouping service algorithm."""
from __future__ import annotations

import dataclasses
from uuid import uuid4

from backend.domain.models import LibraryFile, Recording
from backend.services.grouping_service import _dynamic_threshold, assign_work
from backend.services.normalization import normalize_artist, normalize_title
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.recordings import FakeRecordingRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository


def _make_repos() -> dict:
    return dict(
        artist_repo=FakeArtistRepository(),
        work_repo=FakeWorkRepository(),
        library_file_repo=FakeLibraryFileRepository(),
        recording_repo=FakeRecordingRepository(),
        song_master_repo=FakeSongMasterRepository(),
    )


def _make_file(
    *,
    artist_name: str = "Test Artist",
    track_title: str = "Test Song",
    file_hash: str | None = None,
    recording_mbid: str | None = None,
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=f"/music/{track_title}.mp3",
        file_hash=file_hash or str(uuid4()),
        format="mp3",
        artist_name=artist_name,
        track_title=track_title,
        recording_mbid=recording_mbid,
    )


def _seed_file_in_work(
    repos: dict,
    *,
    artist_name: str,
    track_title: str,
    file_hash: str | None = None,
) -> tuple[LibraryFile, str]:
    """Helper: create a file in the repo that already has a work_id."""
    f = _make_file(
        artist_name=artist_name,
        track_title=track_title,
        file_hash=file_hash,
    )
    work_id = repos["work_repo"].create_local(track_title, "seed-artist")
    f = dataclasses.replace(
        f,
        work_id=work_id,
        normalized_artist_name=normalize_artist(artist_name),
        normalized_title=normalize_title(track_title),
    )
    repos["library_file_repo"].upsert(f)
    return f, work_id


# --- Step 1: Hash shortcut ---


def test_hash_shortcut_inherits_work_id() -> None:
    repos = _make_repos()
    _existing, existing_work = _seed_file_in_work(
        repos,
        artist_name="Test Artist",
        track_title="Test Song",
        file_hash="samehash",
    )
    incoming = _make_file(file_hash="samehash", track_title="New Copy")
    result = assign_work(incoming, **repos)
    assert result == existing_work


def test_hash_shortcut_skips_self() -> None:
    repos = _make_repos()
    f = _make_file(artist_name="Artist", track_title="Song")
    repos["library_file_repo"].upsert(f)  # upserted but no work_id
    result = assign_work(f, **repos)
    assert result is not None  # Should create new work, not find self


# --- Step 2: MBID shortcut ---


def test_mbid_shortcut_inherits_work_id() -> None:
    repos = _make_repos()
    rec = Recording(id="rec-123", title="Test", work_id="work-456")
    repos["recording_repo"].upsert(rec)
    incoming = _make_file(recording_mbid="rec-123")
    result = assign_work(incoming, **repos)
    assert result == "work-456"


# --- Step 3: Fuzzy matching ---


def test_fuzzy_match_same_song_different_version() -> None:
    repos = _make_repos()
    _seed_file_in_work(
        repos, artist_name="Beatles", track_title="Hey Jude",
    )
    incoming = _make_file(
        artist_name="Beatles", track_title="Hey Jude (Remastered)",
    )
    result = assign_work(incoming, **repos)
    # Should match existing work (parenthetical stripped during normalization)
    existing_files = repos["library_file_repo"].get_candidates_by_artist(
        normalize_artist("Beatles"),
    )
    assert result is not None
    assert result == existing_files[0][0]  # Same work_id


def test_different_song_same_artist_gets_new_work() -> None:
    repos = _make_repos()
    _, existing_work = _seed_file_in_work(
        repos, artist_name="Beatles", track_title="Hey Jude",
    )
    incoming = _make_file(artist_name="Beatles", track_title="Let It Be")
    result = assign_work(incoming, **repos)
    assert result is not None
    assert result != existing_work


# --- Step 4: Create local ---


def test_no_match_creates_local_work() -> None:
    repos = _make_repos()
    incoming = _make_file(
        artist_name="New Artist", track_title="Brand New Song",
    )
    result = assign_work(incoming, **repos)
    assert result is not None
    work = repos["work_repo"].get_by_id(result)
    assert work is not None
    assert work.title == "Brand New Song"
    assert work.origin.value == "local"


def test_creates_song_master_for_new_work() -> None:
    repos = _make_repos()
    incoming = _make_file(artist_name="Artist", track_title="Song")
    work_id = assign_work(incoming, **repos)
    assert work_id is not None
    sm = repos["song_master_repo"].get_by_work(work_id)
    assert sm is not None
    assert sm.preferred_file_id == incoming.id


# --- No metadata ---


def test_returns_none_for_missing_artist() -> None:
    repos = _make_repos()
    incoming = _make_file(artist_name="", track_title="Song")
    assert assign_work(incoming, **repos) is None


def test_returns_none_for_missing_title() -> None:
    repos = _make_repos()
    incoming = _make_file(artist_name="Artist", track_title="")
    assert assign_work(incoming, **repos) is None


# --- Dynamic threshold ---


def test_dynamic_threshold_values() -> None:
    assert _dynamic_threshold(3) == 95.0
    assert _dynamic_threshold(5) == 90.0
    assert _dynamic_threshold(9) == 90.0
    assert _dynamic_threshold(10) == 85.0
    assert _dynamic_threshold(25) == 85.0
    assert _dynamic_threshold(30) == 80.0

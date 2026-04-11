"""Tests for the 4-step grouping service algorithm."""
from __future__ import annotations

import dataclasses
from uuid import uuid4

from backend.domain.enums import VersionType
from backend.domain.catalog import Recording
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.grouping_service import (
    GroupingResult,
    _dynamic_threshold,
    _extract_version_info,
    assign_work,
)
from backend.services.normalization import normalize_artist, normalize_title
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.recordings import FakeRecordingRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository


def _make_repos() -> dict:
    work_repo = FakeWorkRepository()
    lib_repo = FakeLibraryFileRepository()
    work_repo.set_library_file_repo(lib_repo)
    return dict(
        artist_repo=FakeArtistRepository(),
        work_repo=work_repo,
        library_file_repo=lib_repo,
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
        audio=AudioMetadata(
            artist_name=artist_name,
            track_title=track_title,
            recording_mbid=recording_mbid,
        ),
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
        audio=dataclasses.replace(
            f.audio,
            normalized_artist_name=normalize_artist(artist_name),
            normalized_title=normalize_title(track_title),
        ),
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
    assert result is not None
    assert isinstance(result, GroupingResult)
    assert result.work_id == existing_work


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
    assert result is not None
    assert result.work_id == "work-456"
    assert result.recording_id == "rec-123"


# --- Step 3: Fuzzy matching ---


def test_fuzzy_match_same_song_different_version() -> None:
    repos = _make_repos()
    _, existing_work = _seed_file_in_work(
        repos, artist_name="Beatles", track_title="Hey Jude",
    )
    incoming = _make_file(
        artist_name="Beatles", track_title="Hey Jude (Remastered)",
    )
    result = assign_work(incoming, **repos)
    assert result is not None
    assert result.work_id == existing_work


def test_different_song_same_artist_gets_new_work() -> None:
    repos = _make_repos()
    _, existing_work = _seed_file_in_work(
        repos, artist_name="Beatles", track_title="Hey Jude",
    )
    incoming = _make_file(artist_name="Beatles", track_title="Let It Be")
    result = assign_work(incoming, **repos)
    assert result is not None
    assert result.work_id != existing_work


# --- Step 4: Create local ---


def test_no_match_creates_local_work() -> None:
    repos = _make_repos()
    incoming = _make_file(
        artist_name="New Artist", track_title="Brand New Song",
    )
    result = assign_work(incoming, **repos)
    assert result is not None
    work = repos["work_repo"].get_by_id(result.work_id)
    assert work is not None
    assert work.title == "Brand New Song"
    assert work.origin.value == "local"


def test_creates_song_master_for_new_work() -> None:
    repos = _make_repos()
    incoming = _make_file(artist_name="Artist", track_title="Song")
    result = assign_work(incoming, **repos)
    assert result is not None
    sm = repos["song_master_repo"].get_by_work(result.work_id)
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


# --- Version extraction ---


def test_extract_version_info_live() -> None:
    base, vtype = _extract_version_info(
        "You Oughta Know (Live/Unplugged)",
    )
    assert base == "You Oughta Know"
    assert vtype == VersionType.LIVE


def test_extract_version_info_no_version() -> None:
    base, vtype = _extract_version_info("You Oughta Know")
    assert base == "You Oughta Know"
    assert vtype == VersionType.ORIGINAL


def test_extract_version_info_preserves_non_version_parens() -> None:
    base, vtype = _extract_version_info("(You Drive Me) Crazy")
    assert base == "(You Drive Me) Crazy"
    assert vtype == VersionType.ORIGINAL


# --- Version-aware grouping ---


def test_version_tag_stripped_before_match() -> None:
    """A live version matches the existing canonical work."""
    repos = _make_repos()
    _, existing_work = _seed_file_in_work(
        repos, artist_name="Alanis", track_title="You Oughta Know",
    )
    incoming = _make_file(
        artist_name="Alanis",
        track_title="You Oughta Know (Live/Unplugged)",
    )
    result = assign_work(incoming, **repos)
    assert result is not None
    assert result.work_id == existing_work


def test_version_creates_recording_with_correct_type() -> None:
    repos = _make_repos()
    incoming = _make_file(
        artist_name="Alanis",
        track_title="You Oughta Know (Live/Unplugged)",
    )
    result = assign_work(incoming, **repos)
    assert result is not None
    assert result.recording_id is not None
    rec = repos["recording_repo"].get_by_id(result.recording_id)
    assert rec is not None
    assert rec.version_type == VersionType.LIVE


def test_no_version_creates_original_recording() -> None:
    repos = _make_repos()
    incoming = _make_file(
        artist_name="Alanis", track_title="You Oughta Know",
    )
    result = assign_work(incoming, **repos)
    assert result is not None
    assert result.recording_id is not None
    rec = repos["recording_repo"].get_by_id(result.recording_id)
    assert rec is not None
    assert rec.version_type == VersionType.ORIGINAL


def test_two_versions_share_work_separate_recordings() -> None:
    repos = _make_repos()
    r1 = assign_work(
        _make_file(
            artist_name="Alanis", track_title="You Oughta Know",
        ),
        **repos,
    )
    # Seed the first file so candidate lookup works
    f1 = _make_file(
        artist_name="Alanis", track_title="You Oughta Know",
    )
    f1 = dataclasses.replace(
        f1,
        work_id=r1.work_id,
        audio=dataclasses.replace(
            f1.audio,
            normalized_artist_name=normalize_artist("Alanis"),
            normalized_title=normalize_title("You Oughta Know"),
        ),
    )
    repos["library_file_repo"].upsert(f1)

    r2 = assign_work(
        _make_file(
            artist_name="Alanis",
            track_title="You Oughta Know (Live/Unplugged)",
        ),
        **repos,
    )
    assert r1 is not None
    assert r2 is not None
    assert r1.work_id == r2.work_id
    assert r1.recording_id != r2.recording_id


def test_step4_creates_work_with_base_title() -> None:
    """When input has a version tag, Step 4 creates work with base title."""
    repos = _make_repos()
    result = assign_work(
        _make_file(
            artist_name="New Artist",
            track_title="Brand New Song (Live)",
        ),
        **repos,
    )
    assert result is not None
    work = repos["work_repo"].get_by_id(result.work_id)
    assert work is not None
    assert work.title == "Brand New Song"

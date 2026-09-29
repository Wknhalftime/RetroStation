"""The after-scan purge's fail-safe guards (fakes): it waits for fingerprints, and it keeps
rows whose file is on disk after all, and rows in a folder that is there but could not be
listed."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.domain.library import AudioHash, AudioMetadata
from backend.services.missing_file_reconciliation_service import (
    file_on_disk,
    folder_unreadable,
    purge_unmatched_missing,
)
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.services.missing_file_helpers import add_file, add_work, fake_repos

_HASH = AudioHash.parse("flac-md5:" + "b" * 32)
_OTHER_HASH = AudioHash.parse("flac-md5:" + "c" * 32)
# Tags that make a file no track of KISS's: only its fingerprint could pair it.
_RETAGGED = AudioMetadata(recording_mbid="rec-2", duration_ms=54_040)


def _refuse_listing(monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    """Make *folder* exist but refuse to be listed, as an unreadable folder does."""
    real = os.scandir

    def scandir(path: str = ".") -> object:
        if Path(path) == folder:
            raise PermissionError(13, "Access is denied", str(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)


def _refuse_stat(monkeypatch: pytest.MonkeyPatch, file_path: Path) -> None:
    """Make the disk refuse to say whether *file_path* is there."""
    real = os.stat

    def stat(path: str, *args: object, **kwargs: object) -> object:
        if Path(path) == file_path:
            raise PermissionError(13, "Access is denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)


def test_a_fingerprinted_row_waits_while_a_present_file_lacks_a_fingerprint() -> None:
    repos = fake_repos()
    add_work(repos)
    gone = add_file(repos, "/lib/gone.flac", missing=True, audio_hash=_HASH)
    add_file(repos, "/lib/moved/new.mp3", missing=False, file_format="mp3", audio=_RETAGGED)

    result = purge_unmatched_missing("/lib", repos, FakeBroadcastTrackIdentityRepository())

    assert repos.files.get_by_id(gone.id) is not None
    assert (result.deleted, result.awaiting_fingerprint) == (0, 1)


def test_a_fingerprinted_row_is_purged_once_every_present_file_has_one() -> None:
    repos = fake_repos()
    add_work(repos)
    gone = add_file(repos, "/lib/gone.flac", missing=True, audio_hash=_HASH)
    add_file(
        repos,
        "/lib/moved/new.mp3",
        missing=False,
        file_format="mp3",
        audio=_RETAGGED,
        audio_hash=_OTHER_HASH,
    )

    result = purge_unmatched_missing("/lib", repos, FakeBroadcastTrackIdentityRepository())

    assert repos.files.get_by_id(gone.id) is None
    assert (result.deleted, result.awaiting_fingerprint) == (1, 0)


def test_a_row_with_no_fingerprint_is_purged_while_others_wait() -> None:
    repos = fake_repos()
    add_work(repos)
    gone = add_file(repos, "/lib/gone.flac", missing=True)
    add_file(repos, "/lib/moved/new.mp3", missing=False, file_format="mp3", audio=_RETAGGED)

    result = purge_unmatched_missing("/lib", repos, FakeBroadcastTrackIdentityRepository())

    assert repos.files.get_by_id(gone.id) is None
    assert (result.deleted, result.awaiting_fingerprint) == (1, 0)


def test_a_row_in_a_folder_that_cannot_be_listed_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    repos = fake_repos()
    add_work(repos)
    kept = add_file(repos, str(locked / "gone.flac"), missing=True)
    gone = add_file(repos, str(tmp_path / "open" / "gone.flac"), missing=True)
    (tmp_path / "open").mkdir()
    _refuse_listing(monkeypatch, locked)

    result = purge_unmatched_missing(str(tmp_path), repos, FakeBroadcastTrackIdentityRepository())

    assert repos.files.get_by_id(kept.id) is not None
    assert repos.files.get_by_id(gone.id) is None
    assert (result.deleted, result.unreadable_folder) == (1, 1)


def test_a_row_whose_folder_is_gone_is_purged(tmp_path: Path) -> None:
    repos = fake_repos()
    add_work(repos)
    gone = add_file(repos, str(tmp_path / "removed" / "gone.flac"), missing=True)

    result = purge_unmatched_missing(str(tmp_path), repos, FakeBroadcastTrackIdentityRepository())

    assert repos.files.get_by_id(gone.id) is None
    assert (result.deleted, result.unreadable_folder) == (1, 0)


def test_folder_unreadable_only_for_a_folder_there_but_unlistable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    _refuse_listing(monkeypatch, locked)

    assert folder_unreadable(str(locked)) is True
    assert folder_unreadable(str(tmp_path)) is False
    assert folder_unreadable(str(tmp_path / "absent")) is False
    assert folder_unreadable(str(a_file)) is False


def test_a_row_whose_file_is_on_disk_is_kept(tmp_path: Path) -> None:
    # The walk missed it (say, a folder above it could not be listed); the file is there.
    on_disk = tmp_path / "locked" / "sub" / "gone.flac"
    on_disk.parent.mkdir(parents=True)
    on_disk.write_bytes(b"audio")
    repos = fake_repos()
    add_work(repos)
    kept = add_file(repos, str(on_disk), missing=True)
    gone = add_file(repos, str(tmp_path / "removed" / "gone.flac"), missing=True)

    result = purge_unmatched_missing(str(tmp_path), repos, FakeBroadcastTrackIdentityRepository())

    assert repos.files.get_by_id(kept.id) is not None
    assert repos.files.get_by_id(gone.id) is None
    assert (result.deleted, result.still_on_disk) == (1, 1)


def test_a_row_whose_file_the_disk_will_not_check_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown = tmp_path / "album" / "gone.flac"
    repos = fake_repos()
    add_work(repos)
    kept = add_file(repos, str(unknown), missing=True)
    _refuse_stat(monkeypatch, unknown)

    result = purge_unmatched_missing(str(tmp_path), repos, FakeBroadcastTrackIdentityRepository())

    assert repos.files.get_by_id(kept.id) is not None
    assert (result.deleted, result.still_on_disk) == (0, 1)


def test_file_on_disk_unless_the_disk_says_it_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    there = tmp_path / "there.flac"
    there.write_bytes(b"audio")
    refused = tmp_path / "refused.flac"
    _refuse_stat(monkeypatch, refused)

    assert file_on_disk(str(there)) is True
    assert file_on_disk(str(refused)) is True
    assert file_on_disk(str(tmp_path / "gone.flac")) is False
    assert file_on_disk(str(tmp_path / "no-folder" / "gone.flac")) is False
    assert file_on_disk(str(there / "under-a-file.flac")) is False

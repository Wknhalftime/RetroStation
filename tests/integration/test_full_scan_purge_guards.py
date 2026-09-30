"""Integration: a full scan's purge waits for fingerprints, and keeps rows in a folder that is
there but could not be listed (the walk skipped it; it did not lose it)."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.enums import FileStatus, MatchStatus
from backend.domain.library import PURGE_MISSING_SETTING, AudioHash, LibraryFile
from backend.domain.system import UserSetting
from backend.services.repository_factory import RepositoryFactory
from tests.integration.missing_file_seed import Conn, full_scan, identity, match, put

_HASH = AudioHash.parse("audio-sha256:" + "a" * 64)
_OTHER_HASH = AudioHash.parse("audio-sha256:" + "d" * 64)


def _stored(repos: RepositoryFactory, path: Path) -> LibraryFile:
    row = repos.library_files.get_by_path(str(path))
    assert row is not None
    return row


def _fingerprint(repos: RepositoryFactory, path: Path, audio_hash: AudioHash) -> None:
    """What the hash backfill records for *path*."""
    row = _stored(repos, path)
    assert row.file_size is not None and row.file_mtime_ns is not None
    assert repos.library_files.set_audio_hash(row.id, audio_hash, row.file_size, row.file_mtime_ns)


def _matched_with_purge_on(conn: Conn, row: LibraryFile) -> UUID:
    """An identity matched to *row*, with library.purge_missing = after_scan; committed."""
    repos = RepositoryFactory(conn)
    matched = identity(repos)
    match(conn, matched, row.id)
    repos.user_settings.upsert(UserSetting(key=PURGE_MISSING_SETTING, value="after_scan"))
    conn.commit()
    return matched


def _refuse_listing(monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    """Make *folder* exist but refuse to be listed, as an unreadable folder does."""
    real = os.scandir

    def scandir(path: str = ".") -> object:
        if Path(path) == folder:
            raise PermissionError(13, "Access is denied", str(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)


def test_a_fingerprinted_row_waits_for_the_backfill(migrated_db: str, tmp_path: Path) -> None:
    put("well_tagged.mp3", tmp_path / "album" / "a.mp3")  # present, not fingerprinted yet
    gone = put("partial_tags.mp3", tmp_path / "album" / "b.mp3")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        full_scan(conn, tmp_path)
        _fingerprint(repos, gone, _HASH)
        matched = _matched_with_purge_on(conn, _stored(repos, gone))

        gone.unlink()
        full_scan(conn, tmp_path)
        row = repos.library_files.get_by_path(str(gone))
        after = repos.broadcast_identities.get_by_id(matched)

    assert row is not None and row.file_status == FileStatus.MISSING
    assert after is not None and after.match_status == MatchStatus.AUTO_MATCHED


def test_a_fingerprinted_row_is_purged_once_the_backfill_is_done(
    migrated_db: str, tmp_path: Path
) -> None:
    kept = put("well_tagged.mp3", tmp_path / "album" / "a.mp3")
    gone = put("partial_tags.mp3", tmp_path / "album" / "b.mp3")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        full_scan(conn, tmp_path)
        _fingerprint(repos, gone, _HASH)
        _fingerprint(repos, kept, _OTHER_HASH)
        _matched_with_purge_on(conn, _stored(repos, gone))

        gone.unlink()
        full_scan(conn, tmp_path)
        row = repos.library_files.get_by_path(str(gone))

    assert row is None


def test_a_row_in_a_folder_that_cannot_be_listed_survives_the_purge(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put("well_tagged.mp3", tmp_path / "open" / "a.mp3")
    locked = tmp_path / "locked"
    hidden = put("partial_tags.mp3", locked / "b.mp3")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        full_scan(conn, tmp_path)
        matched = _matched_with_purge_on(conn, _stored(repos, hidden))

        hidden.unlink()  # the walk sees nothing in the folder, as if it could not list it
        _refuse_listing(monkeypatch, locked)
        full_scan(conn, tmp_path)
        row = repos.library_files.get_by_path(str(hidden))
        after = repos.broadcast_identities.get_by_id(matched)

    assert row is not None and row.file_status == FileStatus.MISSING
    assert after is not None and after.match_status == MatchStatus.AUTO_MATCHED

"""Integration: lookups reconciliation and the matcher read, and what they skip."""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.enums import FileStatus
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.repository_factory import RepositoryFactory

pytestmark = pytest.mark.integration


def _file(
    repos: RepositoryFactory,
    path: str,
    *,
    missing: bool = False,
    track_number: int = 16,
) -> LibraryFile:
    lf = repos.library_files.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=path,
            file_hash=None,
            format="flac",
            audio=AudioMetadata(
                recording_mbid="rec-1",
                normalized_artist_name="samuel l jackson",
                release_title="Pulp Fiction",
                track_number=track_number,
                normalized_title="ezekiel 25 17",
            ),
        )
    )
    if missing:
        repos.library_files.mark_missing(path)
    return lf


def test_get_missing_returns_missing_rows_in_path_order(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        _file(repos, r"D:\m\b.flac", missing=True)
        _file(repos, r"D:\m\a.flac", missing=True)
        _file(repos, r"D:\m\c.flac")

        paths = [f.file_path for f in repos.library_files.get_missing()]

        assert paths == [r"D:\m\a.flac", r"D:\m\b.flac"]
        assert all(f.file_status == FileStatus.MISSING for f in repos.library_files.get_missing())


def test_get_present_by_track_skips_missing_rows_and_other_tracks(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        present = _file(repos, r"D:\m\new.flac")
        _file(repos, r"D:\m\old.flac", missing=True)
        _file(repos, r"D:\m\other.flac", track_number=3)

        found = repos.library_files.get_present_by_track(
            "samuel l jackson",
            "Pulp Fiction",
            16,
            "ezekiel 25 17",
        )

        assert [f.id for f in found] == [present.id]


def test_matcher_lookups_skip_missing_rows(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        present = _file(repos, r"D:\m\new.flac")
        _file(repos, r"D:\m\old.flac", missing=True)

        by_mbid = repos.library_files.get_by_recording_mbid("rec-1")
        by_artist = repos.library_files.get_by_normalized_artist_name("samuel l jackson")

        assert [f.id for f in by_mbid] == [present.id]
        assert [f.id for f in by_artist] == [present.id]

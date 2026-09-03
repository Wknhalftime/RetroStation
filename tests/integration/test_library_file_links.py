"""Integration: library_files upsert must not destroy derived links.

The grouping pass sets ``work_id`` and enrichment sets ``recording_id``.
A re-scan produces a fresh ``LibraryFile`` with neither, and before this
the upsert wrote those NULLs straight over the existing links — every full
scan silently un-grouped and un-enriched the whole library.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.enums import EnrichmentStatus, VersionType
from backend.domain.library import LibraryFile
from backend.services.repository_factory import RepositoryFactory

pytestmark = pytest.mark.integration


def _linked_file(repos: RepositoryFactory, path: str) -> LibraryFile:
    """A file the way it looks after grouping + enrichment have run."""
    artist_id = repos.artists.upsert_local_artist("Prince", "prince")
    work_id = repos.works.create_local("Kiss", artist_id)
    recording_id = repos.recordings.get_or_create_local(
        work_id, VersionType.ORIGINAL.value, "Kiss",
    )
    return LibraryFile(
        id=uuid4(),
        file_path=path,
        file_hash="original-hash",
        format="flac",
        enrichment_status=EnrichmentStatus.ENRICHED,
        recording_id=recording_id,
        work_id=work_id,
        file_size=100,
        file_mtime_ns=5,
    )


def test_reupsert_with_changed_hash_keeps_links_but_resets_enrichment(
    migrated_db: str, tmp_path: Path,
) -> None:
    path = str(tmp_path / "kiss.flac")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        original = _linked_file(repos, path)
        repos.library_files.upsert(original)
        conn.commit()

        # What a re-scan hands the repository after a retag: new hash, no links.
        retagged = LibraryFile(
            id=uuid4(), file_path=path, file_hash="retagged-hash", format="flac",
            file_size=101, file_mtime_ns=6,
        )
        repos.library_files.upsert_write_only(retagged)
        conn.commit()

        got = repos.library_files.get_by_path(path)
        assert got is not None
        assert got.work_id == original.work_id
        assert got.recording_id == original.recording_id
        # Content changed, so enrichment must run again.
        assert got.enrichment_status == EnrichmentStatus.PENDING
        assert got.file_size == 101
        assert got.file_mtime_ns == 6


def test_reupsert_with_same_hash_keeps_everything(migrated_db: str, tmp_path: Path) -> None:
    path = str(tmp_path / "kiss.flac")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        original = _linked_file(repos, path)
        repos.library_files.upsert(original)
        conn.commit()

        same = LibraryFile(
            id=uuid4(), file_path=path, file_hash="original-hash", format="flac",
        )
        repos.library_files.upsert_write_only(same)
        conn.commit()

        got = repos.library_files.get_by_path(path)
        assert got is not None
        assert got.work_id == original.work_id
        assert got.recording_id == original.recording_id
        assert got.enrichment_status == EnrichmentStatus.ENRICHED


def test_update_file_stat_round_trip(migrated_db: str, tmp_path: Path) -> None:
    path = str(tmp_path / "kiss.flac")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        legacy = LibraryFile(id=uuid4(), file_path=path, file_hash="h", format="flac")
        repos.library_files.upsert(legacy)
        conn.commit()

        before = repos.library_files.get_by_path(path)
        assert before is not None
        assert before.file_size is None
        assert before.file_mtime_ns is None

        repos.library_files.update_file_stat(legacy.id, file_size=2048, file_mtime_ns=99)
        conn.commit()

        after = repos.library_files.get_by_path(path)
        assert after is not None
        assert after.file_size == 2048
        assert after.file_mtime_ns == 99

"""Test that upsert preserves enrichment_status when file_hash is unchanged."""
from __future__ import annotations

from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.domain.enums import EnrichmentStatus, FileStatus
from backend.domain.models import LibraryFile


def _make_file(
    *,
    file_path: str = "/music/track.flac",
    file_hash: str = "abc123",
    enrichment_status: EnrichmentStatus = EnrichmentStatus.PENDING,
    file_status: FileStatus = FileStatus.PRESENT,
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=file_path,
        file_hash=file_hash,
        format="flac",
        enrichment_status=enrichment_status,
        file_status=file_status,
        track_title="Test Track",
    )


def test_upsert_same_hash_preserves_enrichment(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        lf = _make_file(file_path="/preserve/same_hash.flac", file_hash="hash_a")
        repo.upsert(lf)
        conn.execute(
            "UPDATE library_files SET enrichment_status = 'enriched' WHERE file_path = %s",
            (lf.file_path,),
        )
        conn.commit()
        lf2 = _make_file(file_path="/preserve/same_hash.flac", file_hash="hash_a", enrichment_status=EnrichmentStatus.PENDING)
        repo.upsert_write_only(lf2)
        conn.commit()
        result = repo.get_by_path("/preserve/same_hash.flac")
        assert result is not None
        assert result.enrichment_status == EnrichmentStatus.ENRICHED


def test_upsert_different_hash_resets_enrichment(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        lf = _make_file(file_path="/preserve/diff_hash.flac", file_hash="old_hash")
        repo.upsert(lf)
        conn.execute(
            "UPDATE library_files SET enrichment_status = 'enriched' WHERE file_path = %s",
            (lf.file_path,),
        )
        conn.commit()
        lf2 = _make_file(file_path="/preserve/diff_hash.flac", file_hash="new_hash", enrichment_status=EnrichmentStatus.PENDING)
        repo.upsert_write_only(lf2)
        conn.commit()
        result = repo.get_by_path("/preserve/diff_hash.flac")
        assert result is not None
        assert result.enrichment_status == EnrichmentStatus.PENDING


def test_upsert_sets_file_status_present(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        lf = _make_file(file_path="/preserve/status.flac")
        repo.upsert(lf)
        conn.execute(
            "UPDATE library_files SET file_status = 'MISSING' WHERE file_path = %s",
            (lf.file_path,),
        )
        conn.commit()
        lf2 = _make_file(file_path="/preserve/status.flac")
        repo.upsert_write_only(lf2)
        conn.commit()
        result = repo.get_by_path("/preserve/status.flac")
        assert result is not None
        assert result.file_status == FileStatus.PRESENT


def test_get_by_folder_path(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        repo.upsert_write_only(_make_file(file_path="/music/jazz/track1.flac"))
        repo.upsert_write_only(_make_file(file_path="/music/jazz/track2.flac"))
        repo.upsert_write_only(_make_file(file_path="/music/rock/track1.flac"))
        conn.commit()
        results = repo.get_by_folder_path("/music/jazz")
        assert len(results) == 2
        paths = {r.file_path for r in results}
        assert paths == {"/music/jazz/track1.flac", "/music/jazz/track2.flac"}


def test_mark_missing(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        lf = _make_file(file_path="/music/gone.flac")
        repo.upsert_write_only(lf)
        conn.commit()
        repo.mark_missing(lf.file_path)
        conn.commit()
        result = repo.get_by_path("/music/gone.flac")
        assert result is not None
        assert result.file_status == FileStatus.MISSING
        assert result.enrichment_status == EnrichmentStatus.PENDING

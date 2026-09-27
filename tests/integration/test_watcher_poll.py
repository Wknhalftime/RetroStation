"""Integration test: full watcher poll cycle with real DB."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.library_folders import PgLibraryFolderRepository
from backend.domain.enums import EnrichmentStatus
from backend.domain.library import LibraryFile
from backend.services.folder_hash_service import diff_tree


def test_first_run_builds_tree_no_changes(migrated_db: str, tmp_path: Path) -> None:
    """First diff_tree call builds folder structure, returns no changes."""
    jazz = tmp_path / "jazz"
    jazz.mkdir()
    (jazz / "track.flac").write_bytes(b"\x00" * 100)

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)
        changes, pending = diff_tree(str(tmp_path), repo)
        conn.commit()

        assert changes == []
        assert repo.has_any() is True


def test_second_run_detects_new_file(migrated_db: str, tmp_path: Path) -> None:
    """After first run, adding a file triggers a change on second run."""
    jazz = tmp_path / "jazz"
    jazz.mkdir()
    (jazz / "track1.flac").write_bytes(b"\x00" * 100)

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)
        diff_tree(str(tmp_path), repo)
        conn.commit()

        (jazz / "track2.flac").write_bytes(b"\x00" * 200)

        changes, pending = diff_tree(str(tmp_path), repo)
        conn.commit()

        assert len(changes) > 0


def test_no_changes_detected_when_unchanged(migrated_db: str, tmp_path: Path) -> None:
    """No changes should be detected when nothing changes between polls."""
    jazz = tmp_path / "jazz"
    jazz.mkdir()
    (jazz / "track.flac").write_bytes(b"\x00" * 100)

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)
        diff_tree(str(tmp_path), repo)
        conn.commit()

        changes, pending = diff_tree(str(tmp_path), repo)
        assert changes == []


def test_upsert_preserves_enrichment_in_full_cycle(migrated_db: str, tmp_path: Path) -> None:
    """An already-enriched file keeps its status after re-upsert with the same size and mtime."""
    jazz = tmp_path / "jazz"
    jazz.mkdir()
    track = jazz / "track.flac"
    track.write_bytes(b"\x00" * 100)
    st = os.stat(track)

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        file_repo = PgLibraryFileRepository(conn)

        lf = LibraryFile(
            id=uuid4(),
            file_path=str(track),
            file_hash="abc123",
            format="flac",
            enrichment_status=EnrichmentStatus.ENRICHED,
            file_size=st.st_size,
            file_mtime_ns=st.st_mtime_ns,
        )
        file_repo.upsert(lf)
        conn.commit()

        # Re-upsert with the same size and mtime (as the watcher's rescan of
        # an unchanged file would do): enrichment survives.
        lf2 = LibraryFile(
            id=uuid4(),
            file_path=str(track),
            file_hash="abc123",
            format="flac",
            enrichment_status=EnrichmentStatus.PENDING,
            file_size=st.st_size,
            file_mtime_ns=st.st_mtime_ns,
        )
        file_repo.upsert_write_only(lf2)
        conn.commit()

        result = file_repo.get_by_path(str(track))
        assert result is not None
        assert result.enrichment_status == EnrichmentStatus.ENRICHED


def test_stage_and_commit_hashes_integration(migrated_db: str, tmp_path: Path) -> None:
    """Staging hashes and committing them updates folder_hash."""
    jazz = tmp_path / "jazz"
    jazz.mkdir()
    (jazz / "track.flac").write_bytes(b"\x00" * 100)

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)
        diff_tree(str(tmp_path), repo)
        conn.commit()

        # Get a folder
        folders = repo.get_all()
        assert len(folders) > 0

        # Stage new hashes
        task_id = "test-task-001"
        repo.stage_hashes([(folders[0].id, "new_staged_hash")], task_id)
        conn.commit()

        count = repo.commit_staged_hashes(task_id)
        conn.commit()

        assert count == 1
        updated = repo.get_by_path(folders[0].full_path)
        assert updated.folder_hash == "new_staged_hash"

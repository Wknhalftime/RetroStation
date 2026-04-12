"""Integration tests for PgRecordingRepository."""
from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.recordings import PgRecordingRepository
from backend.domain.catalog import Recording
from backend.domain.enums import VersionType


def _make_recording(mbid: str, version_type: VersionType = VersionType.ORIGINAL) -> Recording:
    return Recording(
        id=mbid,
        title="Test Track",
        work_id=None,
        version_type=version_type,
        needs_enhancement=False,
    )


def test_upsert_persists_version_type_on_update(migrated_db: str) -> None:
    """Upserting an existing recording with a new version_type must persist the change."""
    mbid = "mbid-test-version-type-upsert"
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgRecordingRepository(conn)

        # Insert the recording with the original version_type
        initial = _make_recording(mbid, version_type=VersionType.ORIGINAL)
        repo.upsert(initial)
        conn.commit()

        # Upsert the same id with a different version_type
        updated = _make_recording(mbid, version_type=VersionType.LIVE)
        repo.upsert(updated)
        conn.commit()

        result = repo.get_by_id(mbid)
        assert result is not None
        assert result.version_type == VersionType.LIVE


def test_upsert_inserts_with_correct_version_type(migrated_db: str) -> None:
    """A fresh upsert stores the supplied version_type."""
    mbid = "mbid-test-version-type-insert"
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgRecordingRepository(conn)

        rec = _make_recording(mbid, version_type=VersionType.REMIX)
        repo.upsert(rec)
        conn.commit()

        result = repo.get_by_id(mbid)
        assert result is not None
        assert result.version_type == VersionType.REMIX


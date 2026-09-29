"""Integration (spec C4): a row a concurrent scan restores mid-purge rolls back only the purge."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.domain.enums import FileStatus, MatchStatus
from backend.domain.library import PURGE_MISSING_SETTING
from backend.domain.system import UserSetting
from backend.services.repository_factory import RepositoryFactory
from tests.integration.missing_file_seed import full_scan, identity, match, put


def _restored(*_args: object) -> bool:
    """delete_missing's answer when the row came back before the delete ran."""
    return False


def test_a_row_restored_during_the_purge_keeps_the_scan(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = put("well_tagged.mp3", tmp_path / "album" / "a.mp3")
    gone = put("partial_tags.mp3", tmp_path / "album" / "b.mp3")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        full_scan(conn, tmp_path)
        gone_row = repos.library_files.get_by_path(str(gone))
        assert gone_row is not None
        matched = identity(repos)
        match(conn, matched, gone_row.id)
        repos.user_settings.upsert(UserSetting(key=PURGE_MISSING_SETTING, value="after_scan"))
        conn.commit()
        gone.unlink()
        # The delete finds the row no longer missing: MissingFileChangedError.
        monkeypatch.setattr(PgLibraryFileRepository, "delete_missing", _restored)

        full_scan(conn, tmp_path)
        rows = (
            repos.library_files.get_by_path(str(kept)),
            repos.library_files.get_by_path(str(gone)),
        )
        after = repos.broadcast_identities.get_by_id(matched)

    assert rows[0] is not None and rows[0].file_status == FileStatus.PRESENT
    assert rows[1] is not None and rows[1].file_status == FileStatus.MISSING  # the scan's mark
    assert after is not None and after.match_status == MatchStatus.AUTO_MATCHED  # rolled back

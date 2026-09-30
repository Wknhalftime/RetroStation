"""Integration: a full scan's final progress reports what the after-scan purge did."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.domain.library import PURGE_MISSING_SETTING
from backend.domain.system import UserSetting
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.library_scan_tasks import PURGE_COUNT_KEYS, _run_scan
from tests.integration.missing_file_seed import Conn, full_scan, identity, match, put

pytestmark = pytest.mark.integration


def _scan_progress(conn: Conn, root: Path) -> dict[str, object]:
    _, _, progress = _run_scan(
        root_path=str(root),
        library_conn=conn,
        repos=RepositoryFactory(conn),
        progress_repo=PgTaskProgressRepository(conn),
        task_id=uuid4().hex,
    )
    conn.commit()
    return progress


def _seed_matched_row_then_lose_it(conn: Conn, root: Path) -> None:
    put("well_tagged.mp3", root / "album" / "a.mp3")
    gone = put("partial_tags.mp3", root / "album" / "b.mp3")
    repos = RepositoryFactory(conn)
    full_scan(conn, root)
    gone_row = repos.library_files.get_by_path(str(gone))
    assert gone_row is not None
    match(conn, identity(repos), gone_row.id)
    conn.commit()
    gone.unlink()


def test_the_scan_progress_carries_the_purge_counts(migrated_db: str, tmp_path: Path) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        _seed_matched_row_then_lose_it(conn, tmp_path)
        RepositoryFactory(conn).user_settings.upsert(
            UserSetting(key=PURGE_MISSING_SETTING, value="after_scan")
        )
        conn.commit()

        progress = _scan_progress(conn, tmp_path)

    assert {k: progress[k] for k in PURGE_COUNT_KEYS} == {
        "missing_purged": 1,
        "purge_matches_released": 1,
        "purge_held_back": 0,
    }


def test_a_scan_that_purges_nothing_reports_no_purge(migrated_db: str, tmp_path: Path) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        _seed_matched_row_then_lose_it(conn, tmp_path)

        progress = _scan_progress(conn, tmp_path)

    assert not set(PURGE_COUNT_KEYS) & set(progress)

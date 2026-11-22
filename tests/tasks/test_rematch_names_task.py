"""The names-scoped re-check the cache fill hands off (MusicBrainz cache fill, phase 4.1 PR 2; SPEC
S7; M4; R6; AUD-R014, AUD-R022, AUD-R091), on PostgreSQL.

``rematch_names_task(names)`` re-checks the log names given (normalized names) as the re-check's
``rewind_wave`` does: an undecided name and its undecided songs go back to pending, and a decided
name keeps its status while its undecided songs go back. Then it hands every playlist with
pending work to artist matching, as ``rematch_undecided_task`` does. It runs on its own task type
(``names_recheck``), so it writes no ``matching_recheck`` row, and the "changed" watermark the
next re-check reads doesn't move.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.domain.enums import TaskStatus, TaskType
from backend.domain.system import TaskProgress
from backend.tasks import huey_app
from backend.tasks.matching_recheck_tasks import rematch_names_task
from tests.integration.cache_fill_rig import Conn, catalog_artist, line, link, log_name, status_of

pytestmark = pytest.mark.integration

WATERMARK = datetime(2026, 11, 20, 6, tzinfo=UTC)


@pytest.fixture
def conn(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Conn]:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    get_settings.cache_clear()
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection
    get_settings.cache_clear()


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    tasks: list[Any] = []
    monkeypatch.setattr(huey_app.huey, "enqueue", tasks.append)
    return tasks


def song_status(conn: Conn, identity: Any) -> str:
    row = conn.execute(
        "SELECT match_status FROM track_identities WHERE id = %s", (identity,)
    ).fetchone()
    assert row is not None
    return str(row["match_status"])


def playlist_of(conn: Conn, identity: Any) -> str:
    row = conn.execute(
        "SELECT DISTINCT playlist_id FROM play_events WHERE identity_id = %s", (identity,)
    ).fetchone()
    assert row is not None
    return str(row["playlist_id"])


def test_it_rewinds_the_names_and_fans_out(conn: Conn, queued: list[Any]) -> None:
    PgTaskProgressRepository(conn).upsert(
        TaskProgress(
            task_id="the-last-recheck",
            task_type=TaskType.MATCHING_RECHECK,
            status=TaskStatus.COMPLETED,
            progress_data={},
            started_at=WATERMARK,
            updated_at=WATERMARK,
            completed_at=WATERMARK,
        )
    )
    alpha = log_name(conn, "ALPHA")
    alpha_song = line(conn, alpha, "One", status="needs_review")
    bravo = log_name(conn, "BRAVO", "auto_rejected")  # not given
    bravo_song = line(conn, bravo, "Two", status="auto_rejected")
    charlie = log_name(conn, "CHARLIE & DELTA", "manual_matched")
    link(conn, charlie, catalog_artist(conn, "Charlie"), origin="manual")
    charlie_song = line(conn, charlie, "Three", status="needs_review")
    conn.commit()

    rematch_names_task.call_local([alpha.normalized, charlie.normalized])
    conn.rollback()

    assert status_of(conn, alpha) == "pending"
    assert song_status(conn, alpha_song) == "pending"
    assert status_of(conn, bravo) == "auto_rejected"
    assert song_status(conn, bravo_song) == "auto_rejected"
    assert status_of(conn, charlie) == "manual_matched"
    assert song_status(conn, charlie_song) == "pending"
    handed = {t.args[0] for t in queued if t.name == "artist_matching_task"}
    assert handed == {playlist_of(conn, alpha_song), playlist_of(conn, charlie_song)}
    repo = PgTaskProgressRepository(conn)
    assert repo.last_completed_started_at(TaskType.MATCHING_RECHECK) == WATERMARK
    rows = conn.execute(
        "SELECT status FROM progress_tracking WHERE task_type = 'names_recheck'"
    ).fetchall()
    assert [r["status"] for r in rows] == ["completed"]

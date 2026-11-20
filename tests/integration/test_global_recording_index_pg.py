"""The global recording index (artist model phase 3b PR 4; SPEC S5, E6; design C40; AUD-R080),
on PostgreSQL.

3a made a recording MBID unique per work (``uq_performances_work_recording_mbid``); the locked
``test_performances_schema_pg.py`` still inserts one MBID under two works. After the merges the
MBID is unique everywhere (C40), but only once no MBID is left under two works:

- the check, ``PerformanceRepository.duplicate_recordings``, lists every recording MBID whose
  performances sit under two or more works, by MBID, each with its works (by id);
- the run-once task ``build_recording_index_task`` (run by name, never queued) does nothing while
  any remain and answers them (E6: a group Lance skipped keeps its duplicate, and the index
  waits); with none left it builds ``uq_performances_recording_mbid`` (unique, on
  ``recording_mbid`` where present) with ``CREATE UNIQUE INDEX CONCURRENTLY``, outside a
  transaction. It is never built by a migration;
- once the index stands, a run does nothing; an invalid one, left by a failed concurrent build,
  is dropped and built again;
- until then the merge list lists the remaining MBIDs with their works (``duplicates``).

Each test drops the index it built: the tests' cleanup truncates tables and keeps indexes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from huey.storage import MemoryStorage
from psycopg.rows import DictRow, dict_row

from backend.config import get_settings
from backend.domain.library import WORK_ARTISTS_CREDITED_SETTING
from backend.domain.performances import DuplicateRecording
from backend.services.repository_factory import RepositoryFactory
from backend.tasks import huey_app
from backend.tasks.recording_index_tasks import build_recording_index_task
from tests.integration import credit_picks_seed as cp
from tests.lifespan_client import lifespan_client

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]

INDEX = "uq_performances_recording_mbid"


def drop_index(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(f"DROP INDEX IF EXISTS {INDEX}")


@pytest.fixture
def conn(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Conn]:
    """The test's connection; the task's own connections use the test database. The index is
    dropped before and after."""
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setattr(huey_app.huey, "enqueue", lambda _task: None)
    get_settings.cache_clear()
    drop_index(migrated_db)
    try:
        with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
            yield connection
    finally:
        drop_index(migrated_db)
        get_settings.cache_clear()


def run(conn: Conn) -> dict[str, Any]:
    """One run of the task. The test's work is committed and its transaction ended first: a
    concurrent build waits for every open transaction."""
    conn.commit()
    result: dict[str, Any] = build_recording_index_task.call_local()
    return result


def index_of(conn: Conn) -> dict[str, Any] | None:
    """The index's oid, validity, uniqueness and definition, or None."""
    row = conn.execute(
        """SELECT c.oid, i.indisvalid AS valid, i.indisunique AS is_unique,
                  pg_get_indexdef(c.oid) AS definition
           FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
           WHERE c.relname = %s""",
        (INDEX,),
    ).fetchone()
    conn.commit()
    return None if row is None else dict(row)


def performance(conn: Conn, work_id: str, recording_mbid: str | None) -> None:
    cp.performance(conn, work_id, [cp.copy(conn, work_id)], recording_mbid=recording_mbid)


def works(conn: Conn, *titles: str) -> list[str]:
    act = cp.artist(conn, "Bon Jovi")
    return [cp.work(conn, title, act) for title in titles]


def duplicates(conn: Conn) -> list[DuplicateRecording]:
    found = RepositoryFactory(conn).performances.duplicate_recordings()
    conn.commit()
    return found


# --- the check and the task -----------------------------------------------------------------------


def test_the_check_lists_mbids_under_two_works(conn: Conn) -> None:
    first, second, third = works(conn, "Always", "Always", "Always")
    performance(conn, first, "rec-b-shared")
    performance(conn, second, "rec-b-shared")
    performance(conn, first, "rec-c-own")
    for work_id in (first, second, third):
        performance(conn, work_id, "rec-a-three")
    performance(conn, first, None)
    performance(conn, second, None)

    assert duplicates(conn) == [
        DuplicateRecording("rec-a-three", tuple(sorted((first, second, third)))),
        DuplicateRecording("rec-b-shared", tuple(sorted((first, second)))),
    ]


def test_the_task_waits_while_any_remain(conn: Conn) -> None:
    """E6: a skipped group keeps its duplicate MBID; the index waits and the task names it."""
    first, second = works(conn, "Always", "Always")
    performance(conn, first, "rec-shared")
    performance(conn, second, "rec-shared")

    assert run(conn) == {"done": False, "built": False, "remaining": ["rec-shared"]}
    assert index_of(conn) is None


def test_the_task_builds_the_index_once_empty(conn: Conn) -> None:
    first, second = works(conn, "Always", "Bed Of Roses")
    performance(conn, first, "rec-own")
    performance(conn, first, None)
    performance(conn, second, None)

    assert run(conn) == {"done": True, "built": True, "remaining": []}

    index = index_of(conn)
    assert index is not None and index["valid"] and index["is_unique"]
    assert "(recording_mbid)" in index["definition"]
    assert "WHERE (recording_mbid IS NOT NULL)" in index["definition"]
    performance(conn, second, None)
    conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        performance(conn, second, "rec-own")
    conn.rollback()


def test_it_runs_once(conn: Conn) -> None:
    performance(conn, works(conn, "Always")[0], "rec-own")
    run(conn)
    built = index_of(conn)

    assert run(conn) == {"done": True, "built": False, "remaining": []}
    assert index_of(conn) == built


def test_an_invalid_index_is_rebuilt(conn: Conn, migrated_db: str) -> None:
    """A concurrent build that met a duplicate leaves an invalid index. Once the duplicate is
    resolved, the task drops it and builds the index again."""
    first, second = works(conn, "Always", "Always")
    performance(conn, first, "rec-shared")
    performance(conn, second, "rec-shared")
    conn.commit()
    with (
        psycopg.connect(migrated_db, autocommit=True) as admin,
        pytest.raises(psycopg.errors.UniqueViolation),
    ):
        admin.execute(
            f"""CREATE UNIQUE INDEX CONCURRENTLY {INDEX}
                ON performances (recording_mbid) WHERE recording_mbid IS NOT NULL"""
        )
    invalid = index_of(conn)
    assert invalid is not None and not invalid["valid"]
    conn.execute(
        "UPDATE performances SET recording_mbid = 'rec-other' WHERE work_id = %s", (second,)
    )

    assert run(conn) == {"done": True, "built": True, "remaining": []}
    rebuilt = index_of(conn)
    assert rebuilt is not None and rebuilt["valid"]


# --- the merge list's section ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def app_client(_migrated_db_url: str) -> Iterator[TestClient]:
    """The app on the test database, its lifespan run once per module; the token is bypassed and
    anything the app queues lands in memory."""
    patch = pytest.MonkeyPatch()
    patch.setattr(huey_app.huey, "storage", MemoryStorage(huey_app.huey.name))
    patch.setenv("DATABASE_URL", _migrated_db_url)
    patch.setenv("RETROSTATION_SKIP_BOOT_MIGRATIONS", "1")
    get_settings.cache_clear()
    from backend.dependencies import get_current_token
    from backend.main import app

    async def _token() -> str:
        return "test-token"

    app.dependency_overrides[get_current_token] = _token
    try:
        with lifespan_client(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        patch.undo()


def test_the_merge_list_shows_the_remaining_groups(app_client: TestClient, conn: Conn) -> None:
    """Until the index is built, the merge list lists each remaining MBID with its works, and
    Library Status counts them (D28)."""
    cp.mark_built(conn)
    cp.set_marker(conn, WORK_ARTISTS_CREDITED_SETTING)
    act = cp.artist(conn, "Bon Jovi")
    always = cp.work(conn, "Always", act)
    roses = cp.work(conn, "Bed Of Roses", act)
    performance(conn, always, "rec-shared")
    performance(conn, roses, "rec-shared")
    conn.commit()

    page = app_client.get("/api/v1/library/merge-list")
    count = app_client.get("/api/v1/library/merge-list/count")

    assert page.status_code == 200
    assert page.json()["duplicates"] == [
        {
            "recording_mbid": "rec-shared",
            "works": sorted(
                [
                    {"work_id": always, "title": "Always"},
                    {"work_id": roses, "title": "Bed Of Roses"},
                ],
                key=lambda w: w["work_id"],
            ),
        }
    ]
    assert count.json()["duplicates"] == 1

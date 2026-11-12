"""A stored performance (performances phase 3a PR 3, SPEC S5; design M28, C71, C27; lock request
question C of PR 2), through the app on PostgreSQL.

A line stores a performance only for a credit (M28): Apply on Credit picks, a version-line pick,
``scripts/apply_credit_picks.py`` and PR 2's hand-off are the only paths, all through
``SongMatchService``. Every stored performance has a preferred copy: storing one that has none
gives it its best copy by the master rules, present before missing; one that has a preferred
copy keeps it (3b's work page lets Lance change it).

A stored performance that is, or later becomes, the work's default (the performance of the
master's file) is skipped by the view, so the line takes the normal path and Set master still
moves it (C71); when Set master moves the master away again, the stored performance plays once
more, its preferred copy being the file Set master chose (PR 2).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from huey.storage import MemoryStorage
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.tasks.credit_pick_tasks import propose_credit_picks_task
from backend.tasks.huey_app import huey
from tests.integration import credit_picks_seed as seed
from tests.integration.credit_picks_seed import Conn, Duet
from tests.lifespan_client import lifespan_client

pytestmark = pytest.mark.integration

# psycopg's async pool needs the selector event loop on Windows (as tests/routers/conftest.py).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

APPLY = "/api/v1/library/credit-picks/apply"


@pytest.fixture(scope="module")
def queue_in_memory() -> Iterator[None]:
    """The hand-offs the app queues land in memory, never in a worker's queue file."""
    patch = pytest.MonkeyPatch()
    patch.setattr(huey, "storage", MemoryStorage(huey.name))
    yield
    patch.undo()


@pytest.fixture(scope="module")
def app_client(_migrated_db_url: str, queue_in_memory: None) -> Iterator[TestClient]:
    """The app on the test database, its lifespan run once per module; the token is bypassed."""
    patch = pytest.MonkeyPatch()
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


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def apply(client: TestClient, applied: list[UUID]) -> dict[str, Any]:
    response = client.post(APPLY, json={"apply": [str(i) for i in applied], "skip": []})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def set_master(client: TestClient, work_id: str, file_id: UUID) -> None:
    response = client.put(
        f"/api/v1/library/works/{work_id}/master", json={"preferred_file_id": str(file_id)}
    )
    assert response.status_code == 200, response.text


# --- storing gives a preferred copy ---------------------------------------------------------------


def test_apply_gives_the_performance_its_best_copy(app_client: TestClient, conn: Conn) -> None:
    """The duet had no preferred copy: Apply makes its flac copy preferred, over the mp3."""
    duet = Duet(conn)
    line = duet.duet_line(conn)
    proposal = seed.propose(conn, line, duet.duet)
    conn.commit()

    apply(app_client, [proposal])

    assert seed.preferred_of(conn, duet.duet) == duet.duet_flac
    assert seed.stored(conn, line) == {duet.duet}
    assert seed.resolved(conn, line) == {(duet.duet_flac, "performance")}


def test_a_present_copy_before_a_missing_one(app_client: TestClient, conn: Conn) -> None:
    duet = Duet(conn)
    conn.execute(
        "UPDATE library_files SET file_status = 'missing' WHERE id = %s", (duet.duet_flac,)
    )
    proposal = seed.propose(conn, duet.duet_line(conn), duet.duet)
    conn.commit()

    apply(app_client, [proposal])

    assert seed.preferred_of(conn, duet.duet) == duet.duet_mp3


def test_a_stored_performance_keeps_its_preferred_copy(app_client: TestClient, conn: Conn) -> None:
    duet = Duet(conn)
    conn.execute(
        "UPDATE performances SET preferred_copy_id = %s WHERE id = %s", (duet.duet_mp3, duet.duet)
    )
    line = duet.duet_line(conn)
    proposal = seed.propose(conn, line, duet.duet)
    conn.commit()

    apply(app_client, [proposal])

    assert seed.preferred_of(conn, duet.duet) == duet.duet_mp3
    assert seed.resolved(conn, line) == {(duet.duet_mp3, "performance")}


# --- only a credit stores one ---------------------------------------------------------------------


def test_only_a_credit_stores_a_performance(app_client: TestClient, conn: Conn) -> None:
    """M28: the proposals store nothing; applying every credit row stores the duet on the duet
    lines only; the solo line and the version line (until Lance picks) store none."""
    duet = Duet(conn)
    seed.mark_built(conn)
    duet_lines = [duet.duet_line(conn), duet.duet_line(conn)]
    solo = duet.solo_line(conn)
    live = duet.duet_line(conn, "Who Says You Can't Go Home (Live)")
    conn.commit()

    propose_credit_picks_task.call_local()
    stored_by_proposals = {line: seed.stored(conn, line) for line in [*duet_lines, solo, live]}
    credit_rows = app_client.get("/api/v1/library/credit-picks").json()["credit"]
    apply(app_client, [UUID(item["id"]) for item in credit_rows])

    assert stored_by_proposals == dict.fromkeys([*duet_lines, solo, live], {None})
    assert {line: seed.stored(conn, line) for line in duet_lines} == dict.fromkeys(
        duet_lines, {duet.duet}
    )
    assert seed.stored(conn, solo) == {None}
    assert seed.stored(conn, live) == {None}


# --- the default ----------------------------------------------------------------------------------


def test_default_follows_set_master(app_client: TestClient, conn: Conn) -> None:
    """C71: once Set master puts the master in the stored performance, the line follows the
    master like any other; once it moves the master out again, the stored performance plays its
    preferred copy, the file Set master chose."""
    duet = Duet(conn)
    stored_line, plain_line = duet.duet_line(conn), duet.solo_line(conn)
    proposal = seed.propose(conn, stored_line, duet.duet)
    conn.commit()
    apply(app_client, [proposal])
    applied = (seed.resolved(conn, stored_line), seed.resolved(conn, plain_line))

    set_master(app_client, duet.work, duet.duet_mp3)
    in_the_duet = (seed.resolved(conn, stored_line), seed.resolved(conn, plain_line))
    set_master(app_client, duet.work, duet.master)
    back_to_solo = (seed.resolved(conn, stored_line), seed.resolved(conn, plain_line))

    assert applied == ({(duet.duet_flac, "performance")}, {(duet.master, "master")})
    assert in_the_duet == ({(duet.duet_mp3, "master")}, {(duet.duet_mp3, "master")})
    assert back_to_solo == ({(duet.duet_mp3, "performance")}, {(duet.master, "master")})
    assert seed.stored(conn, stored_line) == {duet.duet}


def test_a_stored_performance_that_is_the_default_is_still_stored(
    app_client: TestClient, conn: Conn
) -> None:
    """Skipped by the view, never cleared: the store stays for when the master moves."""
    duet = Duet(conn)
    line = duet.duet_line(conn)
    proposal = seed.propose(conn, line, duet.duet)
    conn.commit()
    apply(app_client, [proposal])

    set_master(app_client, duet.work, duet.duet_flac)

    assert seed.stored(conn, line) == {duet.duet}
    assert seed.resolved(conn, line) == {(duet.duet_flac, "master")}

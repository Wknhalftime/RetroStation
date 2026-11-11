"""Every path that keeps masters right keeps preferred copies right too (performances phase 3a
PR 2, SPEC S5's upkeep, E8; design C71).

A performance gets a preferred copy when a line stores it (S5; PR 3's Apply, or a hand-off),
and keeps it. When a path takes that copy out of its performance (a move, split, reassign,
delete, purge or fold) or it goes missing, the path re-picks it inside its own performance:
the best copy left by the master rules (release
status, release type and format, then bitrate, then length; the lower file id on a tie), a
present copy before a missing one. A fold re-points it to the survivor when the survivor is a
copy of the same performance, as it re-points a master. A path never gives a preferred copy to
a performance that has none, except to a performance a line is handed to (E3, in
``test_writers_keep_performances_pg.py``). Deleting a work's master leaves preferred copies alone.

Set master sets the chosen file's performance's preferred copy to that file when the
performance has one (a line stores it); a performance with none keeps none.

E8: a preferred copy that goes missing is re-picked after the scan (``repick_stranded_masters``)
among its performance's present copies; with none present it stays, and (from PR 3's view) the
line reports the unavailable file, never falling back.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from huey.storage import MemoryStorage
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend.config import get_settings
from backend.domain.library import MissingFileSelection
from backend.services import work_curation_service as curation
from backend.services.missing_file_reconciliation_service import (
    delete_missing_files,
    purge_unmatched_missing,
    remap_missing_file,
    repick_stranded_masters,
)
from backend.services.repository_factory import (
    RepositoryFactory,
    reconciliation_repos,
    work_curation_repos,
)
from backend.services.work_move_service import WorkMoveRepos, move_file_to_work
from backend.tasks.huey_app import huey
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import Conn
from tests.lifespan_client import lifespan_client

pytestmark = pytest.mark.integration

# psycopg's async pool needs the selector event loop on Windows (as tests/routers/conftest.py).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

R1 = "0b000000-0000-4000-8000-000000000001"
R2 = "0b000000-0000-4000-8000-000000000002"
A = "0a000000-0000-4000-8000-000000000001"


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


# --- seeds and reads ------------------------------------------------------------------------------


def copy(
    conn: Conn,
    work_id: str,
    fmt: str = "mp3",
    *,
    status: str = "present",
    recording_mbid: str | None = R1,
    path: Path | None = None,
) -> UUID:
    """A copy of R1 on *work_id*, credited to A, in *fmt* (flac scores above mp3)."""
    file_id = seed.library_file(conn, status=status, audio_hash=seed.audio_hash())
    conn.execute(
        """UPDATE library_files
           SET work_id = %s, recording_mbid = %s, track_title = 'Black', format = %s,
               artist_mbids = %s, credit = %s, file_path = COALESCE(%s, file_path)
           WHERE id = %s""",
        (
            work_id,
            recording_mbid,
            fmt,
            [A],
            Jsonb([{"mbid": A, "name": "A", "join": None}]),
            None if path is None else str(path),
            file_id,
        ),
    )
    return file_id


def performance(
    conn: Conn,
    work_id: str,
    files: tuple[UUID, ...],
    *,
    preferred: UUID | None = None,
    recording_mbid: str | None = R1,
) -> UUID:
    row = conn.execute(
        """INSERT INTO performances (work_id, recording_mbid, version_type, length_ms)
           VALUES (%s, %s, 'original', 200000) RETURNING id""",
        (work_id, recording_mbid),
    ).fetchone()
    assert row is not None
    held = UUID(str(row["id"]))
    conn.execute(
        """INSERT INTO performance_credits (performance_id, position, mbid, credited_name, role)
           VALUES (%s, 0, %s, 'A', 'main')""",
        (held, A),
    )
    for file_id in files:
        conn.execute("UPDATE library_files SET performance_id = %s WHERE id = %s", (held, file_id))
    if preferred is not None:
        conn.execute(
            "UPDATE performances SET preferred_copy_id = %s WHERE id = %s", (preferred, held)
        )
        # The line that stores it, matched to the last file, which no test moves.
        match = seed.match(conn, seed.identity(conn), files[-1])
        conn.execute("UPDATE matches SET performance_id = %s WHERE id = %s", (held, match))
    return held


def preferred_of(conn: Conn, performance_id: UUID) -> UUID | None:
    row = conn.execute(
        "SELECT preferred_copy_id FROM performances WHERE id = %s", (performance_id,)
    ).fetchone()
    assert row is not None
    return None if row["preferred_copy_id"] is None else UUID(str(row["preferred_copy_id"]))


def move_repos(conn: Conn) -> WorkMoveRepos:
    repos = RepositoryFactory(conn)
    return WorkMoveRepos(
        files=repos.library_files,
        works=repos.works,
        song_masters=repos.song_masters,
        matches=repos.matches,
        performances=repos.performances,
    )


def curation_repos(conn: Conn) -> curation.WorkCurationRepos:
    return work_curation_repos(RepositoryFactory(conn))


# --- the file leaves its performance --------------------------------------------------------------


def test_move_file_to_work(conn: Conn) -> None:
    """The best copy left, present before missing; a performance with none gains none."""
    work, target = seed.work(conn), seed.work(conn)
    moving = copy(conn, work, "flac")
    missing_flac, present_mp3 = copy(conn, work, "flac", status="missing"), copy(conn, work)
    held = performance(conn, work, (moving, missing_flac, present_mp3), preferred=moving)
    other_moving, other_left = (
        copy(conn, work, recording_mbid=R2),
        copy(conn, work, recording_mbid=R2),
    )
    unstored = performance(conn, work, (other_moving, other_left), recording_mbid=R2)
    moved = RepositoryFactory(conn).library_files

    for file_id in (moving, other_moving):
        row = moved.get_by_id(file_id)
        assert row is not None
        move_file_to_work(row, target, move_repos(conn))

    assert preferred_of(conn, held) == present_mp3
    assert preferred_of(conn, unstored) is None


def test_move_leaves_only_missing_copies(conn: Conn) -> None:
    work, target = seed.work(conn), seed.work(conn)
    moving = copy(conn, work, "flac")
    mp3, flac = copy(conn, work, status="missing"), copy(conn, work, "flac", status="missing")
    held = performance(conn, work, (moving, mp3, flac), preferred=moving)
    row = RepositoryFactory(conn).library_files.get_by_id(moving)
    assert row is not None

    move_file_to_work(row, target, move_repos(conn))

    assert preferred_of(conn, held) == flac


def test_split(conn: Conn) -> None:
    work = seed.work(conn)
    leaving, mp3, flac = copy(conn, work, "flac"), copy(conn, work), copy(conn, work, "flac")
    held = performance(conn, work, (leaving, mp3, flac), preferred=leaving)

    curation.split_file_off(work, leaving, curation_repos(conn))

    assert preferred_of(conn, held) == flac


def test_reassign(conn: Conn) -> None:
    work, target = seed.work(conn), seed.work(conn)
    leaving, left = copy(conn, work, "flac"), copy(conn, work)
    held = performance(conn, work, (leaving, left), preferred=leaving)

    curation.reassign_file(leaving, target, curation_repos(conn))

    assert preferred_of(conn, held) == left


def test_a_tie_goes_to_the_lower_file_id(conn: Conn) -> None:
    work, target = seed.work(conn), seed.work(conn)
    leaving, first, second = copy(conn, work, "flac"), copy(conn, work), copy(conn, work)
    held = performance(conn, work, (leaving, first, second), preferred=leaving)

    curation.reassign_file(leaving, target, curation_repos(conn))

    assert preferred_of(conn, held) == min(first, second)


# --- the file is deleted --------------------------------------------------------------------------


def test_delete_for_file(conn: Conn) -> None:
    """The missing-file delete (``delete_for_file`` on matches, masters and overrides)."""
    work = seed.work(conn)
    gone, left = copy(conn, work, "flac", status="missing"), copy(conn, work)
    held = performance(conn, work, (gone, left), preferred=gone)
    repos = RepositoryFactory(conn)

    delete_missing_files(
        MissingFileSelection(ids=(gone,)), reconciliation_repos(repos), repos.broadcast.identities
    )

    assert preferred_of(conn, held) == left


def test_purge(conn: Conn, tmp_path: Path) -> None:
    """The copy left carries no recording MBID: a present file with the missing one's MBID
    holds its track, and the purge would keep the missing row for a fold."""
    work = seed.work(conn)
    gone = copy(conn, work, "flac", status="missing", path=tmp_path / "gone.flac")
    left = copy(conn, work, recording_mbid=None, path=tmp_path / "left.mp3")
    (tmp_path / "left.mp3").write_bytes(b"")
    held = performance(conn, work, (gone, left), preferred=gone)
    repos = RepositoryFactory(conn)

    purged = purge_unmatched_missing(
        str(tmp_path), reconciliation_repos(repos), repos.broadcast.identities
    )

    assert purged.deleted == 1
    assert preferred_of(conn, held) == left


def test_fold_onto_a_copy_of_the_same_performance(conn: Conn) -> None:
    """Re-pointed to the survivor, as the master is, even with a better copy beside it."""
    work = seed.work(conn)
    folded = copy(conn, work, "flac", status="missing")
    survivor, better = copy(conn, work), copy(conn, work, "flac")
    held = performance(conn, work, (folded, survivor, better), preferred=folded)

    remap_missing_file(folded, survivor, reconciliation_repos(RepositoryFactory(conn)))

    assert preferred_of(conn, held) == survivor


def test_fold_onto_a_copy_elsewhere(conn: Conn) -> None:
    """The survivor holds another performance: the copy is re-picked inside its own."""
    work, kept = seed.work(conn), seed.work(conn)
    folded, left = copy(conn, work, "flac", status="missing"), copy(conn, work)
    held = performance(conn, work, (folded, left), preferred=folded)
    survivor = copy(conn, kept)
    performance(conn, kept, (survivor,))

    remap_missing_file(folded, survivor, reconciliation_repos(RepositoryFactory(conn)))

    assert preferred_of(conn, held) == left


# --- the copy goes missing (E8) -------------------------------------------------------------------


def test_missing_preferred_copy(conn: Conn) -> None:
    """E8: re-picked among the present copies after the scan; with none present it stays."""
    work, other = seed.work(conn), seed.work(conn)
    gone, flac, mp3 = copy(conn, work), copy(conn, work, "flac"), copy(conn, work)
    held = performance(conn, work, (gone, flac, mp3), preferred=gone)
    alone, also_missing = copy(conn, other), copy(conn, other, "flac", status="missing")
    stranded = performance(conn, other, (alone, also_missing), preferred=alone)
    repos = RepositoryFactory(conn)
    for file_id in (gone, alone):
        repos.library_files.mark_missing(seed.file_path(conn, file_id))

    repick_stranded_masters(reconciliation_repos(repos))

    assert preferred_of(conn, held) == flac
    assert preferred_of(conn, stranded) == alone


# --- works: merge and delete_master ---------------------------------------------------------------


def test_merge(conn: Conn) -> None:
    """A target performance that has a preferred copy keeps it; one given a line by the merge
    gets its best present copy."""
    target, source = seed.work(conn), seed.work(conn)
    kept_copy, better = copy(conn, target), copy(conn, target, "flac")
    keeps = performance(conn, target, (kept_copy, better), preferred=kept_copy)
    on_r2 = copy(conn, target, recording_mbid=R2)
    gains = performance(conn, target, (on_r2,), recording_mbid=R2)
    joining = copy(conn, source, "flac")
    performance(conn, source, (joining,), preferred=joining)
    handed = copy(conn, source, recording_mbid=R2), copy(conn, source, "flac", recording_mbid=R2)
    performance(conn, source, handed, preferred=handed[0], recording_mbid=R2)

    curation.merge_works(target, [source], curation_repos(conn))

    assert preferred_of(conn, keeps) == kept_copy
    assert preferred_of(conn, gains) == handed[1]


def test_delete_master_leaves_preferred_copies_alone(conn: Conn) -> None:
    work = seed.work(conn)
    master, preferred = copy(conn, work, "flac"), copy(conn, work)
    held = performance(conn, work, (master, preferred), preferred=preferred)
    seed.song_master(conn, work, master)

    curation.delete_master(work, RepositoryFactory(conn).song_masters)

    assert preferred_of(conn, held) == preferred


# --- Set master -----------------------------------------------------------------------------------


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


def test_set_master(app_client: TestClient, conn: Conn) -> None:
    """The chosen file becomes its performance's preferred copy when it has one; a performance
    with none keeps none."""
    work = seed.work(conn)
    master, stored_copy, chosen = copy(conn, work, "flac"), copy(conn, work), copy(conn, work)
    held = performance(conn, work, (stored_copy, chosen), preferred=stored_copy)
    plain_copy = copy(conn, work, recording_mbid=R2)
    plain = performance(conn, work, (plain_copy,), recording_mbid=R2)
    performance(conn, work, (master,), recording_mbid=None)
    seed.song_master(conn, work, master)
    conn.commit()

    first = app_client.put(
        f"/api/v1/library/works/{work}/master", json={"preferred_file_id": str(chosen)}
    )
    second = app_client.put(
        f"/api/v1/library/works/{work}/master", json={"preferred_file_id": str(plain_copy)}
    )

    assert (first.status_code, second.status_code) == (200, 200)
    assert preferred_of(conn, held) == chosen
    assert preferred_of(conn, plain) is None

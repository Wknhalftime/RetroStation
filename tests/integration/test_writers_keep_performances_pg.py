"""Every writer that sets a file's work also sets its performance (performances phase 3a PR 2,
SPEC S3, E2, E3, E4, D5, D6; design C40, C71; AUD-R032).

One test per writer, through the code the routes and tasks call. After each, every file the
writer grouped or moved holds a performance of its own work, found or created by S2's rule
(the same recording MBID; otherwise the same credit artists, version and a length within 3 s).
The locked seeds insert files without a performance, so NOT NULL can't hold: each test asserts
``performance_id``.

- Every ``work_id`` write clears ``performance_id`` in the same statement (``update_work_id``,
  the merge's bulk move, and an upsert that names another work), so the composite key never
  fails; ``assign_performance`` then sets it (E4). A writer that changes the work and keeps the
  performance fails at the statement.
- A performance that keeps other files stays with them (E2).
- **E3 and D5.** When a writer leaves a performance with no files, each line storing it is
  handed off or released, through ``SongMatchService``, and the empty performance is deleted:
  - handed off (logged ``stored_performance_handed_off``) when every file the performance held
    now sits in one performance, and that performance is in the line's work (the work of the
    file the line is matched to). A folded file sits where its survivor does;
  - otherwise released to the default (logged ``stored_performance_released``): the line keeps
    its match and stores no performance. A deleted file sits nowhere.
  A line the writer carries to another work (its matched file moves) while its stored
  performance stays behind with other files is released too (S5). A release never clears a
  performance's preferred copy.
  A performance a line now stores gets a preferred copy if it had none: its best present copy by
  the master rules (S5).
- **D6, tags only.** A rescan that sees a file's recording MBID tag change re-assigns it in its
  work; an unchanged file keeps its performance. Enrichment's recording link and the curation's
  recording re-points never move a performance.
- The repos bundles carry the performance port as an optional field, ``performances``, and
  every place in ``backend/`` that builds one passes it.
"""

from __future__ import annotations

import ast
import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from huey.storage import MemoryStorage
from mutagen.id3 import ID3, TIT2, TPE1, TXXX
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from structlog.testing import capture_logs

from backend.config import get_settings
from backend.domain.enums import EnrichmentStatus
from backend.domain.library import LibraryFile, MissingFileSelection
from backend.services import work_curation_service as curation
from backend.services.missing_file_reconciliation_service import (
    delete_missing_files,
    purge_unmatched_missing,
    remap_missing_file,
)
from backend.services.repository_factory import (
    RepositoryFactory,
    reconciliation_repos,
    work_curation_repos,
)
from backend.services.work_move_service import WorkMoveRepos, move_file_to_work
from backend.tasks import huey_app
from backend.tasks.huey_app import huey
from backend.tasks.library_watcher_tasks import library_scan_files_task
from tests.integration import stream_seed as seed
from tests.integration.missing_file_seed import full_scan, put
from tests.integration.stream_seed import Conn
from tests.lifespan_client import lifespan_client

pytestmark = pytest.mark.integration

# psycopg's async pool needs the selector event loop on Windows (as tests/routers/conftest.py).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

ROOT = Path(__file__).resolve().parents[2]
R1 = "0b000000-0000-4000-8000-000000000001"
R2 = "0b000000-0000-4000-8000-000000000002"
R3 = "0b000000-0000-4000-8000-000000000003"
A = "0a000000-0000-4000-8000-000000000001"
HANDED_OFF = "stored_performance_handed_off"
RELEASED = "stored_performance_released"


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


# --- seeds and reads ------------------------------------------------------------------------------


def file_on(
    conn: Conn,
    work_id: str | None,
    *,
    recording_mbid: str | None = None,
    duration_ms: int | None = 200_000,
    status: str = "present",
    path: Path | None = None,
) -> UUID:
    """A file on *work_id* titled "Black", credited to artist A, with this MBID and length."""
    file_id = seed.library_file(
        conn, status=status, duration_ms=duration_ms, audio_hash=seed.audio_hash()
    )
    conn.execute(
        """UPDATE library_files
           SET work_id = %s, recording_mbid = %s, track_title = 'Black', artist_mbids = %s,
               credit = %s, file_path = COALESCE(%s, file_path)
           WHERE id = %s""",
        (
            work_id,
            recording_mbid,
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
    recording_mbid: str | None = None,
    preferred: UUID | None = None,
    length_ms: int = 200_000,
) -> UUID:
    """A performance of *work_id* holding *files*, credited to A, as S2 would have built it."""
    row = conn.execute(
        """INSERT INTO performances (work_id, recording_mbid, version_type, length_ms)
           VALUES (%s, %s, 'original', %s) RETURNING id""",
        (work_id, recording_mbid, length_ms),
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
    return held


def line(conn: Conn, file_id: UUID, stores: UUID | None = None) -> UUID:
    """A matched line's match on *file_id*, storing *stores*."""
    match = seed.match(conn, seed.identity(conn), file_id)
    conn.execute("UPDATE matches SET performance_id = %s WHERE id = %s", (stores, match))
    return match


def performance_of(conn: Conn, file_id: UUID) -> UUID | None:
    row = conn.execute(
        "SELECT performance_id FROM library_files WHERE id = %s", (file_id,)
    ).fetchone()
    assert row is not None
    return None if row["performance_id"] is None else UUID(str(row["performance_id"]))


def held_in_own_work(conn: Conn, file_id: UUID) -> dict[str, Any]:
    """The file's performance, asserted to exist and to be of the file's own work."""
    row = conn.execute(
        """SELECT p.id, p.work_id, p.recording_mbid, f.work_id AS file_work
           FROM library_files f JOIN performances p ON p.id = f.performance_id
           WHERE f.id = %s""",
        (file_id,),
    ).fetchone()
    assert row is not None, "the file holds no performance"
    assert row["work_id"] == row["file_work"]
    return dict(row)


def stored(conn: Conn, match: UUID) -> UUID | None:
    row = conn.execute("SELECT performance_id FROM matches WHERE id = %s", (match,)).fetchone()
    assert row is not None
    return None if row["performance_id"] is None else UUID(str(row["performance_id"]))


def preferred_of(conn: Conn, performance_id: UUID) -> UUID | None:
    row = conn.execute(
        "SELECT preferred_copy_id FROM performances WHERE id = %s", (performance_id,)
    ).fetchone()
    assert row is not None
    return None if row["preferred_copy_id"] is None else UUID(str(row["preferred_copy_id"]))


def exists(conn: Conn, performance_id: UUID) -> bool:
    row = conn.execute("SELECT 1 FROM performances WHERE id = %s", (performance_id,)).fetchone()
    return row is not None


def events(logs: list[dict[str, Any]], name: str) -> list[int]:
    """The ``lines`` of each logged *name* event."""
    return [int(e["lines"]) for e in logs if e["event"] == name]


def move_repos(conn: Conn) -> WorkMoveRepos:
    repos = RepositoryFactory(conn)
    return WorkMoveRepos(
        files=repos.library_files,
        works=repos.works,
        song_masters=repos.song_masters,
        matches=repos.matches,
        performances=repos.performances,
    )


def move(conn: Conn, file_id: UUID, work_id: str) -> None:
    moving = RepositoryFactory(conn).library_files.get_by_id(file_id)
    assert moving is not None
    move_file_to_work(moving, work_id, move_repos(conn))


# --- a scanned library ----------------------------------------------------------------------------


def tagged(dest: Path, recording_mbid: str | None) -> Path:
    """A copy of the fixture MP3 at *dest*: Pearl Jam's "Black", with this recording MBID."""
    put("well_tagged.mp3", dest)
    tags = ID3(str(dest))
    tags.clear()
    tags.add(TPE1(encoding=3, text=["Pearl Jam"]))
    tags.add(TIT2(encoding=3, text=["Black"]))
    tags.add(TXXX(encoding=3, desc="MusicBrainz Release Type", text=["album"]))
    if recording_mbid is not None:
        tags.add(TXXX(encoding=3, desc="MusicBrainz Track Id", text=[recording_mbid]))
    tags.save()
    return dest


def by_name(conn: Conn) -> dict[str, UUID]:
    rows = conn.execute("SELECT id, file_path FROM library_files").fetchall()
    return {PureWindowsPath(r["file_path"]).name: UUID(str(r["id"])) for r in rows}


def test_the_scan_groups_new_files_into_performances(conn: Conn, tmp_path: Path) -> None:
    """Grouping (``assign_work``, then ``place_ungrouped_file``): the two copies of one recording
    share a performance; the other recording has its own, in the same work."""
    library = tmp_path / "library"
    tagged(library / "a.mp3", R1)
    tagged(library / "b.mp3", R2)
    tagged(library / "c.mp3", R1)

    full_scan(conn, library)

    files = by_name(conn)
    a, b, c = (held_in_own_work(conn, files[name]) for name in ("a.mp3", "b.mp3", "c.mp3"))
    assert a["id"] == c["id"] != b["id"]
    assert (a["recording_mbid"], b["recording_mbid"]) == (R1, R2)


@pytest.fixture
def task_database(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The task's own connections use the test database; hand-offs stay off the queue."""
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setattr(huey_app.huey, "enqueue", lambda _task: None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("task_database")
def test_the_watcher_groups_a_new_file(conn: Conn, tmp_path: Path) -> None:
    folder = tmp_path / "library"
    tagged(folder / "a.mp3", R1)

    library_scan_files_task.call_local([str(folder)], uuid4().hex)

    assert held_in_own_work(conn, by_name(conn)["a.mp3"])["recording_mbid"] == R1


def test_a_rescan_follows_a_retag(conn: Conn, tmp_path: Path) -> None:
    """D6: b's recording MBID tag changes to a's, so the rescan moves b into a's performance
    and deletes b's emptied one; a, unchanged, keeps its performance."""
    library = tmp_path / "library"
    tagged(library / "a.mp3", R1)
    tagged(library / "b.mp3", R2)
    full_scan(conn, library)
    files = by_name(conn)
    a_before = held_in_own_work(conn, files["a.mp3"])["id"]
    b_before = held_in_own_work(conn, files["b.mp3"])["id"]

    tagged(library / "b.mp3", R1)
    full_scan(conn, library)

    assert held_in_own_work(conn, files["a.mp3"])["id"] == a_before
    assert held_in_own_work(conn, files["b.mp3"])["id"] == a_before
    assert not exists(conn, b_before)


def test_an_upsert_naming_another_work_clears_the_performance(conn: Conn) -> None:
    """The upserts' ``COALESCE(EXCLUDED.work_id, …)``: no work, or the same work, keeps the
    performance; another work clears it in the same statement. (The scan's upserts name no
    work; grouping then places the file.)"""
    work, other = seed.work(conn), seed.work(conn)
    kept = file_on(conn, work, recording_mbid=R1)
    held = performance(conn, work, (kept,), recording_mbid=R1)
    files = RepositoryFactory(conn).library_files
    row = files.get_by_id(kept)
    assert row is not None

    files.upsert(LibraryFile(id=uuid4(), file_path=row.file_path, format="flac", work_id=None))
    after_none = performance_of(conn, kept)
    files.upsert(LibraryFile(id=uuid4(), file_path=row.file_path, format="flac", work_id=work))
    after_same = performance_of(conn, kept)
    files.upsert(LibraryFile(id=uuid4(), file_path=row.file_path, format="flac", work_id=other))

    assert after_none == after_same == held
    assert performance_of(conn, kept) is None


# --- the work moves -------------------------------------------------------------------------------


def test_move_file_to_work(conn: Conn) -> None:
    """Enrichment's and Set master's move: the file joins the target's performance of its MBID;
    the performance it left keeps its other file (E2)."""
    work, target = seed.work(conn), seed.work(conn)
    moving, staying = file_on(conn, work, recording_mbid=R1), file_on(conn, work, recording_mbid=R1)
    left = performance(conn, work, (moving, staying), recording_mbid=R1)
    there = file_on(conn, target, recording_mbid=R1)
    joined = performance(conn, target, (there,), recording_mbid=R1)

    move(conn, moving, target)

    assert held_in_own_work(conn, moving)["id"] == joined
    assert performance_of(conn, staying) == left


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


def test_set_master_moves_a_file_with_its_performance(app_client: TestClient, conn: Conn) -> None:
    """Set master on a file of another work moves it (``choose_master``), as the route wires it."""
    work, other = seed.work(conn), seed.work(conn)
    chosen = file_on(conn, other, recording_mbid=R2)
    # The work's page lists a file of another work only on one of its recordings.
    conn.execute(
        "UPDATE library_files SET recording_id = %s WHERE id = %s",
        (seed.recording(conn, work), chosen),
    )
    performance(conn, other, (chosen,), recording_mbid=R2)
    master = file_on(conn, work, recording_mbid=R1)
    performance(conn, work, (master,), recording_mbid=R1)
    seed.song_master(conn, work, master)
    conn.commit()

    response = app_client.put(
        f"/api/v1/library/works/{work}/master", json={"preferred_file_id": str(chosen)}
    )

    assert response.status_code == 200
    got = held_in_own_work(conn, chosen)
    assert (got["work_id"], got["recording_mbid"]) == (work, R2)


def test_merge(conn: Conn) -> None:
    """The bulk move: each source file joins the target's performance by S2's rule or gets a
    new one; the sources' performances go."""
    target, source = seed.work(conn), seed.work(conn)
    there = file_on(conn, target, recording_mbid=R1)
    joined = performance(conn, target, (there,), recording_mbid=R1)
    same, new = file_on(conn, source, recording_mbid=R1), file_on(conn, source, recording_mbid=R2)
    gone = (
        performance(conn, source, (same,), recording_mbid=R1),
        performance(conn, source, (new,), recording_mbid=R2),
    )

    curation.merge_works(target, [source], work_curation_repos(RepositoryFactory(conn)))

    assert held_in_own_work(conn, same)["id"] == joined
    assert held_in_own_work(conn, new)["recording_mbid"] == R2
    assert not any(exists(conn, p) for p in gone)


def test_partial_move(conn: Conn) -> None:
    """E2, through split: the performance stays with the file left behind, and the split file
    gets a performance in its new work."""
    work = seed.work(conn)
    leaving, staying = (
        file_on(conn, work, recording_mbid=R1),
        file_on(conn, work, recording_mbid=R1),
    )
    held = performance(conn, work, (leaving, staying), recording_mbid=R1)

    split = curation.split_file_off(work, leaving, work_curation_repos(RepositoryFactory(conn)))

    got = held_in_own_work(conn, leaving)
    assert (got["work_id"], got["recording_mbid"]) == (split.new_work_id, R1)
    assert performance_of(conn, staying) == held


def test_reassign(conn: Conn) -> None:
    work, target = seed.work(conn), seed.work(conn)
    leaving, staying = file_on(conn, work, recording_mbid=R1), file_on(conn, work)
    performance(conn, work, (leaving,), recording_mbid=R1)
    performance(conn, work, (staying,))
    there = file_on(conn, target, recording_mbid=R1)
    joined = performance(conn, target, (there,), recording_mbid=R1)

    curation.reassign_file(leaving, target, work_curation_repos(RepositoryFactory(conn)))

    assert held_in_own_work(conn, leaving)["id"] == joined


# --- E3 and D5: the performance a writer empties --------------------------------------------------


def test_empty_performance(conn: Conn) -> None:
    """E3: reassign takes a performance's only file to another work. The line matched to that
    file goes with it, so it is handed off to the file's new performance, which gets the file as
    its preferred copy; the line matched to the master stays in the old work, so it is released.
    The empty performance is deleted."""
    work, target = seed.work(conn), seed.work(conn)
    master = file_on(conn, work)
    performance(conn, work, (master,))
    seed.song_master(conn, work, master)
    duet = file_on(conn, work, recording_mbid=R1)
    emptied = performance(conn, work, (duet,), recording_mbid=R1, preferred=duet)
    follows = line(conn, duet, emptied)
    stays = line(conn, master, emptied)
    unstored = line(conn, master)

    with capture_logs() as logs:
        curation.reassign_file(duet, target, work_curation_repos(RepositoryFactory(conn)))

    landed = held_in_own_work(conn, duet)["id"]
    assert stored(conn, follows) == landed
    assert preferred_of(conn, landed) == duet
    assert stored(conn, stays) is None
    assert stored(conn, unstored) is None
    assert not exists(conn, emptied)
    assert events(logs, HANDED_OFF) == [1]
    assert events(logs, RELEASED) == [1]


def test_merge_hands_off(conn: Conn) -> None:
    """D5: a merge keeps every pick. A source performance whose files all land in one target
    performance hands its lines to it, and that performance keeps its own preferred copy; one
    whose files land in two releases its line."""
    target, source = seed.work(conn), seed.work(conn)
    theirs = file_on(conn, target, recording_mbid=R1)
    also = file_on(conn, target, duration_ms=201_000)
    one = performance(conn, target, (theirs,), recording_mbid=R1, preferred=theirs)
    untagged_there = performance(conn, target, (also,), length_ms=201_000)
    whole = file_on(conn, source, recording_mbid=R1), file_on(conn, source, recording_mbid=R1)
    lands_whole = performance(conn, source, whole, recording_mbid=R1, preferred=whole[0])
    near = file_on(conn, source, duration_ms=201_000)  # 0 ms from untagged_there's length
    far = file_on(conn, source, duration_ms=204_500)  # 3.5 s from it: a new performance
    lands_split = performance(conn, source, (near, far), preferred=near, length_ms=201_000)
    handed = line(conn, whole[1], lands_whole)
    released = line(conn, far, lands_split)

    with capture_logs() as logs:
        curation.merge_works(target, [source], work_curation_repos(RepositoryFactory(conn)))

    assert performance_of(conn, whole[0]) == performance_of(conn, whole[1]) == one
    assert performance_of(conn, near) == untagged_there
    assert held_in_own_work(conn, far)["id"] not in (one, untagged_there)
    assert stored(conn, handed) == one
    assert preferred_of(conn, one) == theirs
    assert stored(conn, released) is None
    assert not exists(conn, lands_whole) and not exists(conn, lands_split)
    assert events(logs, HANDED_OFF) == [1]
    assert events(logs, RELEASED) == [1]


def test_a_line_carried_away_is_released(conn: Conn) -> None:
    """S5: split carries the line matched to the split file to the new work, while the
    performance it stores stays with the other file: released, and the performance keeps its
    preferred copy. A line matched to the file left behind keeps it."""
    work = seed.work(conn)
    leaving, staying = (
        file_on(conn, work, recording_mbid=R1),
        file_on(conn, work, recording_mbid=R1),
    )
    held = performance(conn, work, (leaving, staying), recording_mbid=R1, preferred=staying)
    carried = line(conn, leaving, held)
    behind = line(conn, staying, held)

    with capture_logs() as logs:
        curation.split_file_off(work, leaving, work_curation_repos(RepositoryFactory(conn)))

    assert stored(conn, carried) is None
    assert stored(conn, behind) == held
    assert preferred_of(conn, held) == staying
    assert events(logs, RELEASED) == [1]
    assert events(logs, HANDED_OFF) == []


def test_fold(conn: Conn) -> None:
    """The fold sets no file's work, but deletes the folded row: its only-file performance
    empties, and its line, re-pointed to the survivor in another work, is handed off to the
    survivor's performance."""
    left, kept = seed.work(conn), seed.work(conn)
    folded = file_on(conn, left, recording_mbid=R1, status="missing")
    emptied = performance(conn, left, (folded,), recording_mbid=R1, preferred=folded)
    survivor = file_on(conn, kept, recording_mbid=R1)
    lands = performance(conn, kept, (survivor,), recording_mbid=R1)
    follows = line(conn, folded, emptied)

    with capture_logs() as logs:
        remap_missing_file(folded, survivor, reconciliation_repos(RepositoryFactory(conn)))

    assert held_in_own_work(conn, survivor)["id"] == lands
    assert stored(conn, follows) == lands
    assert preferred_of(conn, lands) == survivor
    assert not exists(conn, emptied)
    assert events(logs, HANDED_OFF) == [1]


def test_delete_missing(conn: Conn) -> None:
    """A deleted file sits nowhere: its emptied performance's line is released (and, as today,
    the line's match on the file goes); a performance with another file stays."""
    work = seed.work(conn)
    gone = file_on(conn, work, recording_mbid=R1, status="missing")
    master = file_on(conn, work, recording_mbid=R2)
    emptied = performance(conn, work, (gone,), recording_mbid=R1, preferred=gone)
    kept = performance(conn, work, (master,), recording_mbid=R2)
    on_master = line(conn, master, emptied)
    repos = RepositoryFactory(conn)

    with capture_logs() as logs:
        deleted = delete_missing_files(
            MissingFileSelection(ids=(gone,)),
            reconciliation_repos(repos),
            repos.broadcast.identities,
        )

    assert deleted.deleted == 1
    assert stored(conn, on_master) is None
    assert not exists(conn, emptied)
    assert performance_of(conn, master) == kept
    assert events(logs, RELEASED) == [1]


def test_purge(conn: Conn, tmp_path: Path) -> None:
    """The purge after a scan deletes as the delete does."""
    work = seed.work(conn)
    gone = file_on(conn, work, recording_mbid=R1, status="missing", path=tmp_path / "gone.mp3")
    master = file_on(conn, work, recording_mbid=R2, path=tmp_path / "kept.mp3")
    (tmp_path / "kept.mp3").write_bytes(b"")
    emptied = performance(conn, work, (gone,), recording_mbid=R1, preferred=gone)
    on_master = line(conn, master, emptied)
    repos = RepositoryFactory(conn)

    purged = purge_unmatched_missing(
        str(tmp_path), reconciliation_repos(repos), repos.broadcast.identities
    )

    assert purged.deleted == 1
    assert stored(conn, on_master) is None
    assert not exists(conn, emptied)


# --- E4: a forgotten writer -----------------------------------------------------------------------


def test_forgotten_writer(conn: Conn) -> None:
    """E4: the PostgreSQL ``work_id`` writes clear ``performance_id`` in the same statement; a
    write that keeps it fails at the statement."""
    work, merged, third, other = (seed.work(conn) for _ in range(4))
    one = file_on(conn, work, recording_mbid=R1)
    two = file_on(conn, merged, recording_mbid=R2)
    three = file_on(conn, third, recording_mbid=R3)
    performance(conn, work, (one,), recording_mbid=R1)
    performance(conn, merged, (two,), recording_mbid=R2)
    performance(conn, third, (three,), recording_mbid=R3)
    repos = RepositoryFactory(conn)

    repos.library_files.update_work_id(one, other)
    repos.work_curation.move_files_of_works([merged], other)

    assert performance_of(conn, one) is None
    assert performance_of(conn, two) is None
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute("UPDATE library_files SET work_id = %s WHERE id = %s", (other, three))


# --- D6: key fields that never move a performance -------------------------------------------------


def test_the_recording_writers_never_move_a_performance(conn: Conn) -> None:
    """Enrichment's recording link (and its clearing on failure), split's and reassign's
    ``set_file_recording`` and the merge's ``move_recording_files`` change ``recording_id``
    only."""
    work = seed.work(conn)
    linked, repointed = file_on(conn, work, recording_mbid=R1), file_on(conn, work)
    held = performance(conn, work, (linked, repointed), recording_mbid=R1)
    recording, other = seed.recording(conn, work), seed.recording(conn, seed.work(conn))
    repos = RepositoryFactory(conn)

    repos.library_files.update_recording_link(linked, recording, EnrichmentStatus.ENRICHED)
    repos.library_files.update_recording_link(linked, None, EnrichmentStatus.FAILED)
    repos.work_curation.set_file_recording(repointed, recording)
    repos.work_curation.move_recording_files(recording, other)

    assert performance_of(conn, linked) == performance_of(conn, repointed) == held


# --- the bundles ----------------------------------------------------------------------------------

BUNDLES = ("WorkMoveRepos", "EnrichmentRepos", "ReconciliationRepos", "WorkCurationRepos")


def bundle_builds() -> list[tuple[str, str, bool]]:
    """(file:line, bundle, passes ``performances=``) of every bundle built in ``backend/``."""
    found = []
    for path in sorted((ROOT / "backend").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else ""
            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in BUNDLES:
                where = f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                passes = any(k.arg == "performances" for k in node.keywords)
                found.append((where, name, passes))
    return found


def test_every_bundle_built_carries_the_performance_port() -> None:
    builds = bundle_builds()

    assert {name for _, name, _ in builds} == set(BUNDLES)
    assert [(where, name) for where, name, passes in builds if not passes] == []

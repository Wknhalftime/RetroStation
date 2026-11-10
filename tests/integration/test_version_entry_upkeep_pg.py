"""Every path that keeps format overrides right carries version entries too (versions phase 2b,
SPEC S3, E1, E2, E7; design C60, D1, D3; AUD-R034).

A version entry is a ``format_overrides`` row with a ``version_text`` and no ``format_name``.
Each path handles the two kinds on its own: a version entry collides only with an entry for the
same (work, version text), a format override only with one for the same (work, format). One test
per path, through the services as the routes and tasks call them:

- create and delete (the works routes' service calls): a format override is created beside the
  work's version entries; deleting "a format override" by a version entry's id finds none;
- the missing-file delete (E1, deleted): every override of the deleted file goes, of both kinds,
  and its lines play the next step;
- the missing-file fold (E1, folded): the folded file's overrides of both kinds follow it to the
  survivor and its work; one the survivor's work already has for the same key wins;
- the works merge (E2): the sources' overrides of both kinds move to the target; the target's
  wins a shared key, then the oldest source's, and each dropped row is counted;
- split, reassign and ``move_file_to_work`` leave both kinds where they were (D1). Split and
  reassign delete an emptied work's with it; ``move_file_to_work`` keeps an emptied work while
  an override names it, as ``delete_if_empty`` does;
- ``delete_if_empty`` keeps a work any override row names, version entries included, and never
  for a state alone (D3);
- the readers (E7): ``list_by_work``, ``get`` and the work page's format overrides return format
  rows only, each with a format name.

``::test_null_format_name_traps`` pins the SQL a NULL ``format_name`` would break: the merge's
``IN`` and ``NOT IN`` over the target's formats, its ``DISTINCT ON (format_name)``, and the fold's
``t.format_name = s.format_name``.

``version_entry_states`` follow the lines (S3, D3; question A of the lock request, Lance
2026-11-08: "Seen only"), so no move makes a seen text read as new on its new work, and a
dismissal on one song never hides a text on another:

- a works merge and the fold join copies of one song. A merge copies every source state to the
  target; the fold copies the old work's state of each version text on a line matched to the
  folded file, and of each entry it moves. Where two states for one text meet: dismissed if
  either was, seen at the later time;
- split, reassign and ``move_file_to_work`` carry lines to another song. The work they move to
  gets, for each version text on a line matched to the file, the old work's state as seen only,
  never a dismissal, at the old work's ``seen_at``. A state it already has keeps its own
  dismissal, or none, seen at the later time;
- the old work keeps all of its own; a deleted work's states go with it.

A line is matched to a file by the view's rule: auto or manual matched, and the file is its best
match with a file. A line whose best match is another file, or whose match on the file awaits
review, brings no state (``::test_split_brings_no_state_from_a_line_matched_elsewhere``,
``::test_reassign_brings_no_state_from_a_line_not_matched``).

E1, missing: an entry whose file goes missing stays, and its lines report the unavailable file
without falling back, as the view does for format overrides and masters.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from huey.storage import MemoryStorage
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.domain.curation import (
    FormatOverrideExistsError,
    FormatOverrideNotFoundError,
    NewFormatOverride,
)
from backend.domain.library import MissingFileSelection
from backend.services import work_curation_service as curation
from backend.services.missing_file_reconciliation_service import (
    delete_missing_files,
    remap_missing_file,
)
from backend.services.repository_factory import (
    RepositoryFactory,
    reconciliation_repos,
    work_curation_repos,
)
from backend.services.work_move_service import WorkMoveRepos, move_file_to_work
from backend.tasks.huey_app import huey
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import Conn, at
from tests.lifespan_client import lifespan_client

pytestmark = pytest.mark.integration

# psycopg's async pool needs the selector event loop on Windows (as tests/routers/conftest.py).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(days=1)
T2 = T0 + timedelta(days=2)
SEEN = None
DISMISSED = "dismissed"

type Key = tuple[str | None, str | None]
"""(format_name, version_text) of one override row."""


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


# --- seeds and reads ------------------------------------------------------------------------------


def file_on(conn: Conn, work_id: str | None, *, status: str = "present") -> UUID:
    """A file whose own ``work_id`` is *work_id*."""
    file_id = seed.library_file(conn, status=status)
    conn.execute("UPDATE library_files SET work_id = %s WHERE id = %s", (work_id, file_id))
    return file_id


def mastered_work(conn: Conn) -> tuple[str, UUID]:
    """A work and its master file."""
    work = seed.work(conn)
    master = file_on(conn, work)
    seed.song_master(conn, work, master)
    return work, master


def entry(
    conn: Conn, work_id: str, text: str, file_id: UUID, *, created_at: datetime | None = None
) -> UUID:
    row = conn.execute(
        """INSERT INTO format_overrides (work_id, version_text, preferred_file_id, created_at)
           VALUES (%s, %s, %s, COALESCE(%s, now())) RETURNING id""",
        (work_id, text, file_id, created_at),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def override(
    conn: Conn,
    work_id: str,
    format_name: str,
    file_id: UUID,
    *,
    created_at: datetime | None = None,
) -> UUID:
    row = conn.execute(
        """INSERT INTO format_overrides (work_id, format_name, preferred_file_id, created_at)
           VALUES (%s, %s, %s, COALESCE(%s, now())) RETURNING id""",
        (work_id, format_name, file_id, created_at),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def state(
    conn: Conn, work_id: str, text: str, kind: str | None = SEEN, seen_at: datetime = T0
) -> None:
    conn.execute(
        """INSERT INTO version_entry_states (work_id, version_text, state, seen_at)
           VALUES (%s, %s, %s, %s)""",
        (work_id, text, kind, seen_at),
    )


def overrides_of(conn: Conn, work_id: str) -> dict[Key, UUID]:
    rows = conn.execute(
        """SELECT format_name, version_text, preferred_file_id FROM format_overrides
           WHERE work_id = %s""",
        (work_id,),
    ).fetchall()
    return {(r["format_name"], r["version_text"]): UUID(str(r["preferred_file_id"])) for r in rows}


def states_of(conn: Conn, work_id: str) -> dict[str, tuple[str | None, datetime]]:
    rows = conn.execute(
        "SELECT version_text, state, seen_at FROM version_entry_states WHERE work_id = %s",
        (work_id,),
    ).fetchall()
    return {r["version_text"]: (r["state"], r["seen_at"]) for r in rows}


def work_exists(conn: Conn, work_id: str) -> bool:
    return conn.execute("SELECT 1 FROM works WHERE id = %s", (work_id,)).fetchone() is not None


def line(conn: Conn, title: str, matched: UUID, *, format_name: str | None = None) -> UUID:
    """One play, at a fresh station of *format_name*, of a fresh line matched to *matched*."""
    station = seed.station(conn, format_name=format_name)
    return seed.matched_play(conn, seed.playlist(conn, station), at("08:00"), matched, title=title)


def plays(conn: Conn, event: UUID) -> tuple[UUID | None, str | None, str | None]:
    """(file_id, file_status, source) of one play."""
    [row] = conn.execute(
        """SELECT file_id, file_status, source FROM play_file_resolution
           WHERE play_event_id = %s""",
        (event,),
    ).fetchall()
    file_id = None if row["file_id"] is None else UUID(str(row["file_id"]))
    return file_id, row["file_status"], row["source"]


def curation_repos(conn: Conn) -> curation.WorkCurationRepos:
    return work_curation_repos(RepositoryFactory(conn))


# --- create and delete ----------------------------------------------------------------------------


def test_create_a_format_override_beside_version_entries(conn: Conn) -> None:
    """A work's version entries don't stop a format override, nor make a second one for the
    same format possible."""
    work, master = mastered_work(conn)
    live = file_on(conn, work)
    entry(conn, work, "live", live)
    repos = curation_repos(conn)

    stored = curation.create_format_override(work, NewFormatOverride("AC", master), repos)
    with pytest.raises(FormatOverrideExistsError):
        curation.create_format_override(work, NewFormatOverride("AC", live), repos)

    assert (stored.format_name, stored.preferred_file_id) == ("AC", master)
    assert overrides_of(conn, work) == {(None, "live"): live, ("AC", None): master}


def test_delete_a_format_override_never_deletes_a_version_entry(conn: Conn) -> None:
    work, master = mastered_work(conn)
    live_entry = entry(conn, work, "live", master)
    ac = override(conn, work, "AC", master)
    overrides = RepositoryFactory(conn).format_overrides

    with pytest.raises(FormatOverrideNotFoundError):
        curation.delete_format_override(work, live_entry, overrides)
    curation.delete_format_override(work, ac, overrides)

    assert overrides_of(conn, work) == {(None, "live"): master}


# --- the readers (E7) -----------------------------------------------------------------------------


def test_mixed_kinds(conn: Conn) -> None:
    """E7: no repository reader returns a version row as a format override."""
    work, master = mastered_work(conn)
    live = file_on(conn, work)
    entry(conn, work, "live", live)
    entry(conn, work, "acoustic", master)
    override(conn, work, "AC", master)
    overrides = RepositoryFactory(conn).format_overrides

    listed = overrides.list_by_work(work)
    got = overrides.get(work, "AC")

    assert [(o.format_name, o.preferred_file_id) for o in listed] == [("AC", master)]
    assert got is not None and got.preferred_file_id == master


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


def test_mixed_kinds_on_the_work_page(app_client: TestClient, conn: Conn) -> None:
    """E7: the work page's format overrides are the format rows only."""
    work, master = mastered_work(conn)
    entry(conn, work, "live", file_on(conn, work))
    override(conn, work, "AC", master)
    conn.commit()

    response = app_client.get(f"/api/v1/library/works/{work}")

    assert response.status_code == 200
    assert [
        (o["format_name"], o["preferred_file_id"]) for o in response.json()["format_overrides"]
    ] == [("AC", str(master))]


# --- E1: the mapped file is deleted, goes missing, or is folded ---------------------------------


def test_deleted(conn: Conn) -> None:
    """The missing-file delete takes every override of the file, of both kinds; the line plays
    the next step."""
    work, master = mastered_work(conn)
    live, ac = file_on(conn, work, status="missing"), file_on(conn, work)
    entry(conn, work, "live", live)
    override(conn, work, "Rock", live)
    override(conn, work, "AC", ac)
    at_ac = line(conn, "Black (Live)", master, format_name="AC")
    at_rock = line(conn, "Black (Live)", master, format_name="Rock")
    repos = RepositoryFactory(conn)

    deleted = delete_missing_files(
        MissingFileSelection(ids=(live,)), reconciliation_repos(repos), repos.broadcast.identities
    )

    assert deleted.deleted == 1
    assert overrides_of(conn, work) == {("AC", None): ac}
    assert plays(conn, at_ac) == (ac, "present", "override")
    assert plays(conn, at_rock) == (master, "present", "master")


def test_missing(conn: Conn) -> None:
    """The entry stays, and its lines report the unavailable file without falling back."""
    work, master = mastered_work(conn)
    live = file_on(conn, work)
    entry(conn, work, "live", live)
    override(conn, work, "AC", master)
    event = line(conn, "Black (Live)", master, format_name="AC")

    RepositoryFactory(conn).library_files.mark_missing(seed.file_path(conn, live))

    assert overrides_of(conn, work) == {(None, "live"): live, ("AC", None): master}
    assert plays(conn, event) == (live, "missing", "version")


def test_folded(conn: Conn) -> None:
    """The folded file's entry and format override follow it to the survivor's work. The
    survivor's work gets the states of the moved entry and of the folded file's lines' texts;
    the work the file left keeps its other entry and all its states. The line, now matched to
    the survivor, plays the survivor as its version."""
    left, left_master = mastered_work(conn)
    folded = file_on(conn, left, status="missing")
    entry(conn, left, "live", folded)
    override(conn, left, "AC", folded)
    entry(conn, left, "acoustic", left_master)
    state(conn, left, "live", SEEN, T0)
    state(conn, left, "lv", DISMISSED, T1)
    state(conn, left, "acoustic", DISMISSED, T1)
    state(conn, left, "demo", SEEN, T0)
    kept, survivor = mastered_work(conn)
    event = line(conn, "Black (Live)", folded)
    line(conn, "Black (Lv)", folded)
    line(conn, "Black (Demo)", left_master)

    remap_missing_file(folded, survivor, reconciliation_repos(RepositoryFactory(conn)))

    assert overrides_of(conn, kept) == {(None, "live"): survivor, ("AC", None): survivor}
    assert overrides_of(conn, left) == {(None, "acoustic"): left_master}
    assert states_of(conn, kept) == {"live": (SEEN, T0), "lv": (DISMISSED, T1)}
    assert states_of(conn, left) == {
        "live": (SEEN, T0),
        "lv": (DISMISSED, T1),
        "acoustic": (DISMISSED, T1),
        "demo": (SEEN, T0),
    }
    assert plays(conn, event) == (survivor, "present", "version")


def test_fold_collision(conn: Conn) -> None:
    """The survivor's work wins a shared key of either kind, and the fold raises nothing. Where
    its state and a moved one meet: dismissed if either was, seen at the later time. The work
    the file left, emptied, goes with its states."""
    left = seed.work(conn)
    folded = file_on(conn, left, status="missing")
    entry(conn, left, "live", folded)
    entry(conn, left, "lv", folded)
    entry(conn, left, "unplugged", folded)
    override(conn, left, "AC", folded)
    state(conn, left, "live", SEEN, T0)
    state(conn, left, "lv", DISMISSED, T0)
    state(conn, left, "unplugged", SEEN, T0)
    kept, survivor = mastered_work(conn)
    theirs = file_on(conn, kept)
    entry(conn, kept, "live", theirs)
    override(conn, kept, "AC", theirs)
    state(conn, kept, "live", DISMISSED, T1)
    state(conn, kept, "lv", SEEN, T2)
    state(conn, kept, "unplugged", DISMISSED, T1)
    line(conn, "Black (Live)", folded)

    remap_missing_file(folded, survivor, reconciliation_repos(RepositoryFactory(conn)))

    assert overrides_of(conn, kept) == {
        (None, "live"): theirs,
        (None, "lv"): survivor,
        (None, "unplugged"): survivor,
        ("AC", None): theirs,
    }
    assert states_of(conn, kept) == {
        "live": (DISMISSED, T1),
        "lv": (DISMISSED, T2),
        "unplugged": (DISMISSED, T1),
    }
    assert not work_exists(conn, left)
    assert states_of(conn, left) == {}


# --- E2: the works merge --------------------------------------------------------------------------


def test_merge(conn: Conn) -> None:
    """Both kinds move to the target, the target's winning a shared key. Every source state is
    copied to the target; where two meet, dismissed if either was, seen at the later time."""
    target, target_master = mastered_work(conn)
    entry(conn, target, "live", target_master)
    override(conn, target, "Rock", target_master)
    state(conn, target, "live", SEEN, T2)
    state(conn, target, "unplugged", DISMISSED, T0)
    source, source_master = mastered_work(conn)
    acoustic = file_on(conn, source)
    entry(conn, source, "live", source_master)
    entry(conn, source, "acoustic", acoustic)
    override(conn, source, "AC", source_master)
    state(conn, source, "live", DISMISSED, T0)
    state(conn, source, "acoustic", SEEN, T0)
    state(conn, source, "demo", DISMISSED, T0)
    state(conn, source, "unplugged", SEEN, T1)
    event = line(conn, "Black (Acoustic)", source_master)

    merged = curation.merge_works(target, [source], curation_repos(conn))

    assert merged.dropped_override_count == 1
    assert overrides_of(conn, target) == {
        (None, "live"): target_master,
        (None, "acoustic"): acoustic,
        ("Rock", None): target_master,
        ("AC", None): source_master,
    }
    assert states_of(conn, target) == {
        "live": (DISMISSED, T2),
        "acoustic": (SEEN, T0),
        "demo": (DISMISSED, T0),
        "unplugged": (DISMISSED, T1),
    }
    assert plays(conn, event) == (acoustic, "present", "version")


def test_merge_collision(conn: Conn) -> None:
    """E2: both works map the same text; the target's entry wins, the drop is counted, the
    source's other entry moves, and the source's line now plays the target's file."""
    target, _ = mastered_work(conn)
    theirs = file_on(conn, target)
    entry(conn, target, "live", theirs)
    source, source_master = mastered_work(conn)
    acoustic = file_on(conn, source)
    entry(conn, source, "live", file_on(conn, source))
    entry(conn, source, "acoustic", acoustic)
    event = line(conn, "Black (Live)", source_master)

    merged = curation.merge_works(target, [source], curation_repos(conn))

    assert merged.dropped_override_count == 1
    assert overrides_of(conn, target) == {(None, "live"): theirs, (None, "acoustic"): acoustic}
    assert plays(conn, event) == (theirs, "present", "version")


def test_null_format_name_traps(conn: Conn) -> None:
    """The SQL a NULL ``format_name`` breaks, in one merge:

    - the target holds a version entry, so its formats include NULL: ``IN`` over them must still
      drop the source's format the target has, and ``NOT IN`` must not drop every other row;
    - ``DISTINCT ON (format_name)`` would fold the sources' version entries into one: each text
      moves;
    - two sources sharing a key: the oldest source's row wins, for a text as for a format.
    """
    target, target_master = mastered_work(conn)
    entry(conn, target, "unplugged", target_master)
    override(conn, target, "Rock", target_master)
    older, older_master = mastered_work(conn)
    newer, newer_master = mastered_work(conn)
    for text in ("live", "acoustic", "demo"):
        entry(conn, older, text, older_master, created_at=T0)
    override(conn, older, "AC", older_master, created_at=T0)
    override(conn, older, "Rock", older_master, created_at=T0)
    override(conn, older, "Jazz", older_master, created_at=T0)
    entry(conn, newer, "live", newer_master, created_at=T1)
    override(conn, newer, "AC", newer_master, created_at=T1)

    merged = curation.merge_works(target, [newer, older], curation_repos(conn))

    assert merged.dropped_override_count == 3
    assert overrides_of(conn, target) == {
        (None, "unplugged"): target_master,
        ("Rock", None): target_master,
        (None, "live"): older_master,
        (None, "acoustic"): older_master,
        (None, "demo"): older_master,
        ("AC", None): older_master,
        ("Jazz", None): older_master,
    }


def test_merge_states_of_two_sources(conn: Conn) -> None:
    """Two sources hold a state for one text and the target none: dismissed if either was,
    seen at the later time."""
    target, _ = mastered_work(conn)
    first, _ = mastered_work(conn)
    second, _ = mastered_work(conn)
    state(conn, first, "live", DISMISSED, T0)
    state(conn, second, "live", SEEN, T1)
    state(conn, first, "lv", SEEN, T0)
    state(conn, second, "lv", SEEN, T2)

    curation.merge_works(target, [second, first], curation_repos(conn))

    assert states_of(conn, target) == {"live": (DISMISSED, T1), "lv": (SEEN, T2)}


# --- D1: split, reassign and move_file_to_work ------------------------------------------------


def test_split(conn: Conn) -> None:
    """D1: both kinds stay on the old work, even those naming the file split off, while it
    survives, and go with it when it empties. The new work gets the old work's state of each
    text on the split file's lines as seen only, never a dismissal, also when the old work is
    deleted; the old work keeps all of its own."""
    work, master = mastered_work(conn)
    leaving = file_on(conn, work)
    entry(conn, work, "live", leaving)
    override(conn, work, "AC", leaving)
    state(conn, work, "live", SEEN, T0)
    state(conn, work, "demo", DISMISSED, T1)
    state(conn, work, "acoustic", SEEN, T0)
    line(conn, "Black (Live)", leaving)
    line(conn, "Black (Demo)", master)
    lone = seed.work(conn)
    only = file_on(conn, lone)
    entry(conn, lone, "live", only)
    override(conn, lone, "AC", only)
    state(conn, lone, "acoustic", DISMISSED, T1)
    state(conn, lone, "live", SEEN, T0)
    line(conn, "Black (Acoustic)", only)
    repos = curation_repos(conn)

    split = curation.split_file_off(work, leaving, repos)
    emptied = curation.split_file_off(lone, only, repos)

    assert overrides_of(conn, work) == {(None, "live"): leaving, ("AC", None): leaving}
    assert states_of(conn, work) == {
        "live": (SEEN, T0),
        "demo": (DISMISSED, T1),
        "acoustic": (SEEN, T0),
    }
    assert overrides_of(conn, split.new_work_id) == {}
    assert states_of(conn, split.new_work_id) == {"live": (SEEN, T0)}
    assert emptied.old_work_deleted
    assert overrides_of(conn, lone) == {}
    assert states_of(conn, lone) == {}
    assert overrides_of(conn, emptied.new_work_id) == {}
    assert states_of(conn, emptied.new_work_id) == {"acoustic": (SEEN, T1)}


def test_reassign(conn: Conn) -> None:
    """D1, as for split: the target gets each text as seen only. A state it already has keeps
    its own dismissal, or none, seen at the later time."""
    work, _ = mastered_work(conn)
    leaving = file_on(conn, work)
    entry(conn, work, "live", leaving)
    override(conn, work, "AC", leaving)
    state(conn, work, "live", DISMISSED, T0)
    state(conn, work, "demo", SEEN, T0)
    line(conn, "Black (Live)", leaving)
    lone = seed.work(conn)
    only = file_on(conn, lone)
    entry(conn, lone, "live", only)
    state(conn, lone, "acoustic", SEEN, T1)
    line(conn, "Black (Acoustic)", only)
    target, _ = mastered_work(conn)
    state(conn, target, "live", SEEN, T2)
    state(conn, target, "acoustic", DISMISSED, T0)
    repos = curation_repos(conn)

    curation.reassign_file(leaving, target, repos)
    emptied = curation.reassign_file(only, target, repos)

    assert overrides_of(conn, work) == {(None, "live"): leaving, ("AC", None): leaving}
    assert states_of(conn, work) == {"live": (DISMISSED, T0), "demo": (SEEN, T0)}
    assert overrides_of(conn, target) == {}
    assert states_of(conn, target) == {"live": (SEEN, T2), "acoustic": (DISMISSED, T1)}
    assert emptied.old_work_deleted
    assert overrides_of(conn, lone) == {}
    assert states_of(conn, lone) == {}


def test_move_file_to_work(conn: Conn) -> None:
    """D1: enrichment's and Set master's move leaves both kinds behind, so a line on the old
    work still plays the moved file (today's gap for format overrides, ARCHITECTURE.md); a
    work emptied by the move stays while an entry names it. The target gets the old work's
    state of each text on the moved file's lines as seen only; one it already has keeps its own
    dismissal, seen at the later time. The old work keeps its own."""
    work, master = mastered_work(conn)
    moving = file_on(conn, work)
    entry(conn, work, "live", moving)
    override(conn, work, "AC", moving)
    state(conn, work, "live", SEEN, T0)
    state(conn, work, "acoustic", SEEN, T1)
    event = line(conn, "Black (Live)", master)
    line(conn, "Black (Acoustic)", moving)
    lone = seed.work(conn)
    only = file_on(conn, lone)
    entry(conn, lone, "live", only)
    state(conn, lone, "demo", DISMISSED, T0)
    line(conn, "Black (Demo)", only)
    target, _ = mastered_work(conn)
    state(conn, target, "acoustic", DISMISSED, T0)
    repos = RepositoryFactory(conn)
    move = WorkMoveRepos(
        files=repos.library_files,
        works=repos.works,
        song_masters=repos.song_masters,
        matches=repos.matches,
    )

    for file_id in (moving, only):
        moved = repos.library_files.get_by_id(file_id)
        assert moved is not None
        move_file_to_work(moved, target, move)

    assert overrides_of(conn, work) == {(None, "live"): moving, ("AC", None): moving}
    assert overrides_of(conn, target) == {}
    assert plays(conn, event) == (moving, "present", "version")
    assert work_exists(conn, lone)
    assert overrides_of(conn, lone) == {(None, "live"): only}
    assert states_of(conn, work) == {"live": (SEEN, T0), "acoustic": (SEEN, T1)}
    assert states_of(conn, lone) == {"demo": (DISMISSED, T0)}
    assert states_of(conn, target) == {"acoustic": (DISMISSED, T1), "demo": (SEEN, T0)}


def test_split_brings_no_state_from_a_line_matched_elsewhere(conn: Conn) -> None:
    """The matched-line rule: a line with a weaker match on the split file, its best match on the
    master, brings the new work no state; a line whose best match is the split file does."""
    work, master = mastered_work(conn)
    leaving = file_on(conn, work)
    state(conn, work, "live", SEEN, T0)
    state(conn, work, "demo", SEEN, T1)
    weaker = seed.identity(conn, title="Black (Live)")
    seed.match(conn, weaker, master, confidence=0.9)
    seed.match(conn, weaker, leaving, confidence=0.5)
    seed.play(conn, seed.playlist(conn, seed.station(conn)), weaker, at("08:00"))
    line(conn, "Black (Demo)", leaving)

    split = curation.split_file_off(work, leaving, curation_repos(conn))

    assert states_of(conn, split.new_work_id) == {"demo": (SEEN, T1)}
    assert states_of(conn, work) == {"live": (SEEN, T0), "demo": (SEEN, T1)}


def test_reassign_brings_no_state_from_a_line_not_matched(conn: Conn) -> None:
    """The matched-line rule: a line whose match on the reassigned file awaits review (a
    suggestion) brings the target no state; an auto or a manual match does."""
    work, _ = mastered_work(conn)
    leaving = file_on(conn, work)
    state(conn, work, "demo", SEEN, T0)
    state(conn, work, "live", SEEN, T1)
    state(conn, work, "acoustic", SEEN, T2)
    suggested = seed.identity(conn, title="Black (Demo)", status="needs_review")
    seed.match(conn, suggested, leaving)
    seed.play(conn, seed.playlist(conn, seed.station(conn)), suggested, at("08:00"))
    line(conn, "Black (Live)", leaving)
    manual = seed.identity(conn, title="Black (Acoustic)", status="manual_matched")
    seed.match(conn, manual, leaving)
    target, _ = mastered_work(conn)

    curation.reassign_file(leaving, target, curation_repos(conn))

    assert states_of(conn, target) == {"live": (SEEN, T1), "acoustic": (SEEN, T2)}


# --- D3: delete_if_empty --------------------------------------------------------------------------


def test_delete_if_empty(conn: Conn) -> None:
    """A version entry keeps its work, as a format override does; a state alone doesn't, and
    goes with the work."""
    elsewhere = seed.work(conn)
    their_file = file_on(conn, elsewhere)
    named = seed.work(conn)
    entry(conn, named, "live", their_file)
    stated = seed.work(conn)
    state(conn, stated, "live", DISMISSED)
    works = RepositoryFactory(conn).works

    assert not works.delete_if_empty(named)
    assert works.delete_if_empty(stated)
    assert overrides_of(conn, named) == {(None, "live"): their_file}
    assert states_of(conn, stated) == {}

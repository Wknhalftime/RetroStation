"""The merge log's rollback map and the restore (artist model phase 3b PR 3, SPEC S10, Q18;
AUD-R079).

Until Lance accepts 3b, every merge keeps a rollback map in the merge log
(``work_merge_rows``): one entry per row it changed, in any of the tables it may write (works,
artists and their names, recordings, files, performances and their credits, masters,
overrides of both kinds, version-entry states, matches, Credit picks' proposals), cascades
included, with the row as it was before the merge (NULL for a row the merge inserted). The log's
tables hold no foreign key to ``performances`` (0051's rollback drops that table), and the map
none to ``works`` (it keeps rows of works the merge deleted).

``backend/db/rollback_work_merges.sql`` (run by hand, ``psql -1``, like the other rollbacks)
restores every logged merge, newest first, and marks each rolled back: the merged rows come
back as they were, the rows the merges inserted go, and so do their pins, stored lines and
redirects. ``backend/db/rollback_0054_work_merge_log.sql`` drops the log; its test runs it
inside one transaction that is rolled back.

Each test empties the log first, since the restore restores every merge it holds.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.integration import credit_picks_seed as cp
from tests.integration import work_merge_seed as seed
from tests.integration.credit_picks_seed import Conn
from tests.integration.work_merge_seed import Cover, Snapshot, TwoWorks

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0054_work_merge_log"
ROLLBACK = ROOT / "backend" / "db" / f"rollback_{VERSION}.sql"
LOG_TABLES = ("work_merges", "work_merge_rows", "work_redirects")


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        seed.fresh_log(connection)
        yield connection


class Busy:
    """A merge that writes every kind of row: the Cover (two acts), with an AC override and a
    version-entry state on each work, a line storing Aguilera's performance before the merge
    (which the merge hands off), a Credit picks proposal on that performance, an unlinked
    Aguilera line (pinned) and a third source work by a local act whose one copy holds no
    performance (so the act ends up credited nowhere and is deleted, with its names)."""

    def __init__(self, conn: Conn) -> None:
        self.cover = world = Cover(conn)
        target_edit = world.copy_of(conn, world.labelle, world.target)
        source_edit = world.copy_of(conn, world.aguilera, world.source)
        cp.override(conn, world.target, "AC", target_edit)
        cp.override(conn, world.source, "AC", source_edit)
        for work_id in (world.target, world.source):
            conn.execute(
                """INSERT INTO version_entry_states (work_id, version_text, state, seen_at)
                   VALUES (%s, 'live', NULL, now())""",
                (work_id,),
            )
        at = seed.station(conn)
        self.stored_line = world.aguilera_line(conn, at)
        conn.execute(
            "UPDATE performances SET preferred_copy_id = %s WHERE id = %s",
            (world.aguilera_master, world.aguilera),
        )
        cp.store(conn, self.stored_line, world.aguilera)
        self.unlinked_line = world.aguilera_line(conn, at, linked=False)
        cp.propose(conn, self.unlinked_line, world.aguilera)
        self.local_act = f"artist-{uuid4()}"
        conn.execute(
            """INSERT INTO artists (id, name, sort_name, origin) VALUES (%s, 'Labelle', 'Labelle',
               'local')""",
            (self.local_act,),
        )
        conn.execute(
            """INSERT INTO artist_names (artist_id, name, normalized_name, source)
               VALUES (%s, 'Labelle', 'labelle', 'tag')""",
            (self.local_act,),
        )
        # 3b PR 5b (D19, R5): the local act is credited only by the third work's performance,
        # a copy of LaBelle's recording. Merged, the copy joins LaBelle's performance, the
        # emptied one goes with its credit, and the act, credited nowhere, goes too.
        shared = "0b000000-0000-4000-8000-0000000000c9"
        conn.execute(
            "UPDATE performances SET recording_mbid = %s WHERE id = %s", (shared, world.labelle)
        )
        conn.execute(
            "UPDATE library_files SET recording_mbid = %s WHERE id = %s",
            (shared, world.labelle_master),
        )
        self.third = cp.work(conn, "Lady Marmalade")
        third_copy = cp.copy(
            conn, self.third, fmt="mp3", title="Lady Marmalade", recording_mbid=shared
        )
        cp.performance(
            conn, self.third, [third_copy], credit=[self.local_act], recording_mbid=shared
        )

    @property
    def sources(self) -> list[str]:
        return [self.cover.source, self.third]


def changed_keys(before: Snapshot, after: Snapshot) -> dict[str, tuple[set[str], set[str]]]:
    """Per table: (keys of rows the merge updated or deleted, keys of rows it inserted)."""
    found: dict[str, tuple[set[str], set[str]]] = {}
    for table, rows in before.items():
        now = after[table]
        touched = {k for k, row in rows.items() if now.get(k) != row}
        inserted = set(now) - set(rows)
        if touched or inserted:
            found[table] = (touched, inserted)
    return found


# --- the map --------------------------------------------------------------------------------------


def dumped(rows: Iterable[object]) -> set[str]:
    return {json.dumps(r, sort_keys=True) for r in rows}


def test_the_map_holds_every_moved_row(conn: Conn) -> None:
    """For each table, the map holds every row the merge updated or deleted, as it was, and an
    entry with no row for each row it inserted. Any other row it holds is one the merge wrote
    and left as it was (restoring it changes nothing)."""
    world = Busy(conn)
    before = seed.snapshot(conn)

    done = seed.merged(conn, world.cover.target, world.sources)

    after = seed.snapshot(conn)
    changed = changed_keys(before, after)
    assert {"works", "artists", "artist_names", "library_files", "performances"} <= set(changed)
    entries = seed.map_of(conn, done.merge_id)
    for table in seed.KEYS:
        touched, inserted = changed.get(table, (set(), set()))
        kept = [e["before"] for e in entries if e["table_name"] == table]
        images = dumped([b for b in kept if b is not None])
        expected = dumped([before[table][k] for k in touched])
        assert expected <= images, table
        assert images - expected <= dumped(after[table].values()), table
        assert sum(b is None for b in kept) >= len(inserted), table
    assert {e["table_name"] for e in entries} <= set(seed.KEYS)


# --- the restore ----------------------------------------------------------------------------------


def test_the_rollback_restores_one_merge(conn: Conn) -> None:
    world = Busy(conn)
    before = seed.snapshot(conn)
    done = seed.merged(conn, world.cover.target, world.sources)

    seed.restore(conn)

    assert seed.snapshot(conn) == before
    [logged] = seed.merges_of(conn, world.cover.target)
    assert (logged["id"], logged["status"]) == (done.merge_id, "rolled_back")


def test_the_rollback_restores_merges_newest_first(conn: Conn) -> None:
    """A into B, then B into C: undone in that order's reverse, every row is as it was before
    either."""
    a, b = TwoWorks(conn), TwoWorks(conn)
    c = cp.work(conn, "Always", a.act)
    cp.copy(conn, c, title="Always")
    line = seed.heard(conn, a.source_master, seed.station(conn), title="Always")
    before = seed.snapshot(conn)
    seed.merged(conn, a.target, [a.source])
    seed.merged(conn, b.target, [a.target, b.source])
    seed.merged(conn, c, [b.target])

    seed.restore(conn)

    assert seed.snapshot(conn) == before
    assert seed.playing(conn, line) == {(a.source_master, "master")}
    statuses = conn.execute("SELECT DISTINCT status FROM work_merges").fetchall()
    assert [s["status"] for s in statuses] == ["rolled_back"]


def test_it_restores_pins_stored_lines_and_redirects(conn: Conn) -> None:
    """After the restore no line pins a file, the stored line stores its performance of the
    source again, and no merged-away id redirects."""
    world = Busy(conn)
    seed.merged(conn, world.cover.target, world.sources)
    assert seed.pin_of(conn, world.unlinked_line) == world.cover.aguilera_master
    assert seed.redirects(conn, world.sources) != {}

    seed.restore(conn)

    assert seed.pin_of(conn, world.unlinked_line) is None
    assert seed.stored_of(conn, world.stored_line) == world.cover.aguilera
    assert seed.redirects(conn, world.sources) == {}
    assert seed.playing(conn, world.unlinked_line) == {(world.cover.aguilera_master, "master")}


# --- the schema -----------------------------------------------------------------------------------


def test_the_log_has_no_fk_to_performances(conn: Conn) -> None:
    """0051's rollback drops ``performances``, so nothing in the log may hold a key to it; and
    the map keeps rows of deleted works, so it holds none to ``works``."""
    rows = conn.execute(
        """SELECT conrelid::regclass::text AS source, confrelid::regclass::text AS target
           FROM pg_constraint
           WHERE contype = 'f' AND conrelid::regclass::text = ANY(%s)""",
        (list(LOG_TABLES),),
    ).fetchall()
    keys = {(r["source"], r["target"]) for r in rows}

    assert {target for _, target in keys}.isdisjoint({"performances", "performance_credits"})
    assert {target for source, target in keys if source == "work_merge_rows"} <= {"work_merges"}


@contextmanager
def before_0054(url: str) -> Iterator[Conn]:
    """The test database without 0054, in one transaction rolled back at the end."""
    conn = psycopg.connect(url, row_factory=dict_row)
    try:
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        yield conn
    finally:
        conn.rollback()
        conn.close()


def test_the_rollback_undoes_0054(migrated_db: str) -> None:
    with before_0054(migrated_db) as conn:
        tables = conn.execute(
            "SELECT to_regclass(t) AS found FROM unnest(%s::text[]) AS t", (list(LOG_TABLES),)
        ).fetchall()
        applied = conn.execute(
            "SELECT count(*) AS n FROM schema_migrations WHERE version = %s", (VERSION,)
        ).fetchone()

        assert [t["found"] for t in tables] == [None, None, None]
        assert applied is not None and applied["n"] == 0

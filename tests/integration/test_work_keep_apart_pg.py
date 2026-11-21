"""Lance's lasting "Keep apart" mark on PostgreSQL (artist model phase 3b PR 5c; SPEC D40, D57,
S3, S10, E4; scope check Q4, Q6, Q7, Q8, Q11, R2, R5; AUD-R089).

Migration 0056 adds ``work_keep_apart``: one row per pair of works Lance keeps apart, its two
work ids in plain string order (``first_work_id < second_work_id`` under ``COLLATE "C"``, the
key), ``kept_at`` (now) and a ``note`` ('' unless the writer sets one). Each work id is a
foreign key to ``works`` with ON DELETE CASCADE, and an index on ``second_work_id`` serves the
lookup from that side. The table has no foreign key to ``performances``. Its rollback,
``backend/db/rollback_0056_work_keep_apart.sql``, drops it and forgets 0056.

Who writes a mark, besides the merge list's button (``test_merge_list_keep_apart.py``):

- **Split and reassign mark their own pair (Q7)**, while the work the file left still exists:
  a split marks (the work left, the new work), noted "Split off <file name>"; a reassign marks
  (the work left, the target), noted "Reassigned <file name> to <target title>". A split or
  reassign that empties the work it left marks nothing. Nothing else that moves a file marks.
- **A merge carries its sources' marks onto the target (Q4)**: each mark of a merged-away work
  is re-pointed to the merged work, a pair that would join the merged work to itself is
  dropped, and of two marks that land on one pair the earlier kept stays, with its note. So a
  Library merge of a kept pair goes through and its mark goes (D57). The marks a merge moves
  are in its rollback map, and S10's restore puts them back.

And who is refused: **the guard (Q6, E4)** won't merge a group holding a kept pair (any two of
its works) for a late MusicBrainz work link, nor for the merge list (a second check behind
Apply's plan). It answers ``MergeRefused`` with no changes and the pairs it holds, merges and
logs nothing in the merge log, and logs ``work_merge_kept_apart``. The Library's merge goes
through (D57).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row
from structlog.testing import capture_logs

from backend.domain.work_merge_planner import WorkPair
from backend.domain.work_merges import MergeDone, MergeReason, MergeRefused
from backend.services.repository_factory import RepositoryFactory, work_curation_repos
from backend.services.work_curation_service import (
    WorkCurationRepos,
    reassign_file,
    split_file_off,
)
from backend.services.work_move_service import WorkMoveRepos, move_file_to_work
from tests.integration import credit_picks_seed as cp
from tests.integration import keep_apart_seed as ka
from tests.integration import work_merge_seed as seed
from tests.integration.credit_picks_seed import Conn
from tests.integration.work_merge_seed import TwoWorks

pytestmark = pytest.mark.integration

ROOT = seed.ROOT
MIGRATION = ROOT / "backend" / "db" / "migrations" / f"{ka.VERSION}.sql"
ROLLBACK = ROOT / "backend" / "db" / f"rollback_{ka.VERSION}.sql"
KEPT_APART_EVENT = "work_merge_kept_apart"


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def repos(conn: Conn) -> WorkCurationRepos:
    return work_curation_repos(RepositoryFactory(conn))


def other_work(conn: Conn, title: str = "Always (Live)") -> str:
    """A third work of its own, with one copy."""
    work_id = cp.work(conn, title)
    cp.copy(conn, work_id, title=title)
    return work_id


def work_with_one_copy(conn: Conn, title: str = "Always") -> tuple[str, UUID]:
    act = cp.artist(conn, "Bon Jovi")
    work_id = cp.work(conn, title, act)
    copy = cp.copy(conn, work_id, title=title)
    cp.performance(conn, work_id, [copy], credit=[act])
    seed.master(conn, work_id, copy)
    return work_id, copy


# --- the table (Q11, R5) --------------------------------------------------------------------


def test_the_table(conn: Conn) -> None:
    columns = {
        r["column_name"]: r
        for r in conn.execute(
            """SELECT column_name, data_type, is_nullable, column_default
                 FROM information_schema.columns WHERE table_name = %s""",
            (ka.TABLE,),
        ).fetchall()
    }
    assert set(columns) == {"first_work_id", "second_work_id", "kept_at", "note"}
    assert columns["first_work_id"]["data_type"] == "text"
    assert columns["second_work_id"]["data_type"] == "text"
    assert columns["kept_at"]["data_type"] == "timestamp with time zone"
    assert columns["note"]["data_type"] == "text"
    assert {c: columns[c]["is_nullable"] for c in columns} == dict.fromkeys(columns, "NO")
    assert "now()" in str(columns["kept_at"]["column_default"])
    assert str(columns["note"]["column_default"]).startswith("''")

    key = conn.execute(
        """SELECT array_agg(a.attname ORDER BY k.n) AS columns
             FROM pg_constraint c
             CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k (attnum, n)
             JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            WHERE c.conrelid = %s::regclass AND c.contype = 'p'""",
        (ka.TABLE,),
    ).fetchone()
    assert key is not None and key["columns"] == ["first_work_id", "second_work_id"]

    foreign = conn.execute(
        """SELECT a.attname AS col, c.confrelid::regclass::text AS refers, c.confdeltype
             FROM pg_constraint c
             JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
            WHERE c.conrelid = %s::regclass AND c.contype = 'f'""",
        (ka.TABLE,),
    ).fetchall()
    assert {(f["col"], f["refers"], f["confdeltype"]) for f in foreign} == {
        ("first_work_id", "works", "c"),
        ("second_work_id", "works", "c"),
    }

    indexed = conn.execute(
        """SELECT 1 FROM pg_index i
             JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
            WHERE i.indrelid = %s::regclass AND a.attname = 'second_work_id'""",
        (ka.TABLE,),
    ).fetchall()
    assert indexed, "no index leads with second_work_id"


@pytest.mark.parametrize(
    "first, second", [("w-b", "w-a"), ("w-a", "w-a")], ids=["out-of-order", "one-work"]
)
def test_a_pair_out_of_order_or_of_one_work_is_refused(conn: Conn, first: str, second: str) -> None:
    for work_id in {first, second}:
        conn.execute("INSERT INTO works (id, title) VALUES (%s, 'Always')", (work_id,))

    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            f"INSERT INTO {ka.TABLE} (first_work_id, second_work_id) VALUES (%s, %s)",
            (first, second),
        )


def test_the_order_is_plain_string_order(conn: Conn) -> None:
    """``COLLATE "C"``: a hyphen sorts before a capital letter, as in Python, whatever the
    database's own collation (a word-sort collation ignores the hyphen)."""
    for work_id in ("work-b", "workA"):
        conn.execute("INSERT INTO works (id, title) VALUES (%s, 'Always')", (work_id,))

    assert ka.keep(conn, "workA", "work-b") == ("work-b", "workA")
    assert WorkPair.of("workA", "work-b") == WorkPair("work-b", "workA")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            f"INSERT INTO {ka.TABLE} (first_work_id, second_work_id) VALUES ('workA', 'work-b')"
        )


@pytest.mark.parametrize("side", ["first", "second"])
def test_either_work_deleted_takes_its_marks(conn: Conn, side: str) -> None:
    """The work deleted is the pair's first or second; the survivor's other mark stays."""
    a, b, c = (cp.work(conn, f"Always {n}") for n in range(3))
    first, second = ka.keep(conn, a, b)
    gone, survivor = (first, second) if side == "first" else (second, first)
    other = ka.keep(conn, survivor, c)

    conn.execute("DELETE FROM works WHERE id = %s", (gone,))

    assert ka.kept(conn) == {other}


def test_the_rollback_drops_the_table_and_forgets_0056(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        try:
            conn.execute(ROLLBACK.read_text(encoding="utf-8"))
            gone = conn.execute("SELECT to_regclass(%s) IS NULL AS gone", (ka.TABLE,)).fetchone()
            forgotten = conn.execute(
                "SELECT count(*) AS n FROM schema_migrations WHERE version = %s", (ka.VERSION,)
            ).fetchone()
        finally:
            conn.rollback()

    assert gone is not None and gone["gone"] is True
    assert forgotten is not None and forgotten["n"] == 0


def test_the_migration_names_no_performances() -> None:
    """Locked tests drop ``performances`` with a plain DROP TABLE (rollback_0051), so no 3b
    table may hold a foreign key to it."""
    text = MIGRATION.read_text(encoding="utf-8").lower()
    assert "performances" not in text
    assert "begin;" not in text and "commit;" not in text


# --- split and reassign mark their own pair (Q7, Q8) ----------------------------------------


def test_a_split_marks_the_work_it_left_and_the_new_work(conn: Conn) -> None:
    world = TwoWorks(conn)
    name = ka.file_name(conn, world.source_copy)

    split = split_file_off(world.source, world.source_copy, repos(conn))

    pair = ka.ordered(world.source, split.new_work_id)
    assert ka.kept(conn) == {pair}
    assert ka.marks(conn)[pair]["note"] == ka.split_note(name)


def test_a_reassign_marks_the_work_it_left_and_the_target(conn: Conn) -> None:
    world = TwoWorks(conn)
    name = ka.file_name(conn, world.source_copy)

    reassign_file(world.source_copy, world.target, repos(conn))

    pair = ka.ordered(world.source, world.target)
    assert ka.kept(conn) == {pair}
    assert ka.marks(conn)[pair]["note"] == ka.reassign_note(name, "Always")


def test_a_split_that_empties_its_work_marks_nothing(conn: Conn) -> None:
    work_id, only = work_with_one_copy(conn)

    split = split_file_off(work_id, only, repos(conn))

    assert split.old_work_deleted is True
    assert ka.kept(conn) == set()


def test_enrichments_move_marks_nothing(conn: Conn) -> None:
    """Only Lance's split and reassign mark (Q7): enrichment moving a copy to another work is
    no decision of his."""
    world = TwoWorks(conn)
    factory = RepositoryFactory(conn)
    moving = factory.library_files.get_by_id(world.source_copy)
    assert moving is not None

    move_file_to_work(
        moving,
        world.target,
        WorkMoveRepos(
            files=factory.library_files,
            works=factory.works,
            song_masters=factory.song_masters,
            matches=factory.matches,
            performances=factory.performances,
        ),
    )

    assert seed.work_of(conn, world.source_copy) == world.target
    assert ka.kept(conn) == set()


def test_the_one_deletion_rule_still_deletes_a_marked_empty_work(conn: Conn) -> None:
    """R5: a mark isn't one of the references that keep an emptied work (it cascades), so the
    sweep's rule deletes the work, and its marks go with it."""
    emptied = cp.work(conn, "Always (Demo)")
    ka.keep(conn, emptied, other_work(conn))

    assert RepositoryFactory(conn).works.delete_if_empty(emptied) is True

    assert not seed.work_exists(conn, emptied)
    assert ka.kept(conn) == set()


def test_a_reassign_that_empties_its_work_marks_nothing(conn: Conn) -> None:
    left, only = work_with_one_copy(conn, "Always (Demo)")
    target = TwoWorks(conn).target

    moved = reassign_file(only, target, repos(conn))

    assert moved.old_work_deleted is True
    assert ka.kept(conn) == set()


# --- a merge carries its sources' marks onto the target (Q4, D57, S10) -----------------------


def test_a_merge_re_points_a_sources_marks_to_the_merged_work(conn: Conn) -> None:
    world = TwoWorks(conn)
    live = other_work(conn)
    ka.keep(conn, world.source, live, note="Split off always-live.mp3")

    seed.merged(conn, world.target, [world.source])

    pair = ka.ordered(world.target, live)
    assert ka.kept(conn) == {pair}
    assert ka.marks(conn)[pair]["note"] == "Split off always-live.mp3"


def test_of_two_marks_landing_on_one_pair_the_earlier_stays(conn: Conn) -> None:
    world = TwoWorks(conn)
    live = other_work(conn)
    earlier = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    later = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
    ka.keep(conn, world.target, live, note="the target's own", at=later)
    ka.keep(conn, world.source, live, note="the source's", at=earlier)

    seed.merged(conn, world.target, [world.source])

    pair = ka.ordered(world.target, live)
    assert ka.marks(conn) == {pair: {"note": "the source's", "kept_at": earlier}}


def test_a_library_merge_of_a_kept_pair_goes_through_and_its_mark_goes(conn: Conn) -> None:
    """D57: Lance picked the works himself on the artist page."""
    world = TwoWorks(conn)
    ka.keep(conn, world.target, world.source, note=ka.MERGE_LIST_NOTE)

    done = seed.merge(conn, world.target, [world.source], reason=MergeReason.LIBRARY)

    assert isinstance(done, MergeDone)
    assert not seed.work_exists(conn, world.source)
    assert ka.kept(conn) == set()


def test_the_restore_puts_a_merges_moved_marks_back(conn: Conn) -> None:
    """S10: the marks a merge re-points are in its rollback map."""
    seed.fresh_log(conn)
    world = TwoWorks(conn)
    live = other_work(conn)
    before = ka.keep(conn, world.source, live, note="kept")
    seed.merged(conn, world.target, [world.source])
    assert ka.kept(conn) == {ka.ordered(world.target, live)}

    seed.restore(conn)

    assert seed.work_exists(conn, world.source)
    assert ka.marks(conn).keys() == {before}
    assert ka.marks(conn)[before]["note"] == "kept"


# --- the guard refuses a kept pair for a work link and the merge list (Q6, E4) --------------


@pytest.mark.parametrize("reason", [MergeReason.WORK_LINK, MergeReason.MERGE_LIST])
def test_the_guard_refuses_a_kept_pair(conn: Conn, reason: MergeReason) -> None:
    world = TwoWorks(conn)
    line = seed.heard(conn, world.source_master, seed.station(conn), title="Always")
    pair = WorkPair(*ka.keep(conn, world.target, world.source))

    with capture_logs() as logs:
        outcome = seed.merge(conn, world.target, [world.source], reason=reason)

    assert isinstance(outcome, MergeRefused)
    assert outcome.changes == ()
    assert outcome.kept_apart == (pair,)
    assert seed.work_exists(conn, world.source)
    assert seed.pin_of(conn, line) is None
    assert seed.merges_of(conn, world.target) == []
    assert ka.kept(conn) == {(pair.first_work_id, pair.second_work_id)}
    [event] = [e for e in logs if e["event"] == KEPT_APART_EVENT]
    assert event["target_id"] == world.target
    assert event["reason"] == reason.value


def test_the_guard_refuses_two_sources_kept_apart(conn: Conn) -> None:
    """Any two works of the group: here two sources kept apart from each other, the target
    from neither."""
    world = TwoWorks(conn)
    live = other_work(conn, "Always")
    pair = WorkPair(*ka.keep(conn, world.source, live))

    outcome = seed.merge(conn, world.target, [world.source, live], reason=MergeReason.WORK_LINK)

    assert isinstance(outcome, MergeRefused)
    assert outcome.kept_apart == (pair,)
    assert seed.work_exists(conn, world.source) and seed.work_exists(conn, live)


def test_a_work_link_merge_with_no_kept_pair_goes_through(conn: Conn) -> None:
    """A mark elsewhere changes nothing."""
    world = TwoWorks(conn)
    ka.keep(conn, other_work(conn), other_work(conn, "Bed Of Roses"))

    outcome = seed.merge(conn, world.target, [world.source], reason=MergeReason.WORK_LINK)

    assert isinstance(outcome, MergeDone)
    assert outcome.result is not None

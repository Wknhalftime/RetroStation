"""Migration 0052's ``credit_pick_proposals`` (performances phase 3a PR 3, SPEC S8; design C57,
C64; AUD-R039's 3a check is ``tests/tasks/test_credit_repoint_proposals.py``).

One row per log line (``identity_id``, unique): the line's best match when it was proposed
(``match_id``), the performance proposed (``performance_id``, none for a version line or a tie),
``kind`` (``credit``, ``version_line``, ``tie``), ``reason`` (``choose_performance``'s:
``credit`` or ``shared_artist``), ``candidates`` (the performances Lance may pick, a uuid
array), ``state`` (``proposed``, ``skipped``, ``applied``, ``picked``), ``proposed_at`` and
``decided_at``, which is set exactly when the row is decided.

Delete rules: a deleted line takes its proposal with it; a deleted match or performance leaves
the proposal, with no match or performance (it is then stale, and Apply skips it). A
performance a proposal names can still be deleted, so PR 2's writers keep working.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.integration import credit_picks_seed as seed
from tests.integration.credit_picks_seed import Conn, Duet

pytestmark = pytest.mark.integration


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def columns(conn: Conn) -> dict[str, tuple[str, str]]:
    """column -> (udt_name, is_nullable) of ``credit_pick_proposals``."""
    rows = conn.execute(
        """SELECT column_name, udt_name, is_nullable FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'credit_pick_proposals'"""
    ).fetchall()
    return {r["column_name"]: (r["udt_name"], r["is_nullable"]) for r in rows}


def fails(conn: Conn, sql: str, params: tuple[object, ...]) -> bool:
    """Whether *sql* is refused by a constraint; the connection is usable afterwards."""
    try:
        with conn.transaction():
            conn.execute(sql, params)
    except psycopg.errors.IntegrityError:
        return True
    return False


def insert(*more: str) -> str:
    """An INSERT of (identity_id, kind, reason, state, *more*), one parameter each."""
    names = ["identity_id", "kind", "reason", "state", *more]
    marks = ", ".join(["%s"] * len(names))
    return f"INSERT INTO credit_pick_proposals ({', '.join(names)}) VALUES ({marks})"


def test_the_table(conn: Conn) -> None:
    assert columns(conn) == {
        "id": ("uuid", "NO"),
        "identity_id": ("uuid", "NO"),
        "match_id": ("uuid", "YES"),
        "performance_id": ("uuid", "YES"),
        "kind": ("text", "NO"),
        "reason": ("text", "NO"),
        "candidates": ("_uuid", "NO"),
        "state": ("text", "NO"),
        "proposed_at": ("timestamptz", "NO"),
        "decided_at": ("timestamptz", "YES"),
    }


def test_one_row_per_line(conn: Conn) -> None:
    duet = Duet(conn)
    line = duet.duet_line(conn)
    seed.propose(conn, line, duet.duet)

    assert fails(conn, insert(), (line, "credit", "credit", "proposed"))


def test_kinds_reasons_and_states(conn: Conn) -> None:
    duet = Duet(conn)
    line = duet.duet_line(conn)
    sql = insert()

    assert fails(conn, sql, (line, "version", "credit", "proposed"))
    assert fails(conn, sql, (line, "credit", "default", "proposed"))
    assert fails(conn, sql, (line, "credit", "no_performer", "proposed"))
    assert fails(conn, sql, (line, "credit", "credit", "stale"))
    for kind in ("credit", "version_line", "tie"):
        for reason in ("credit", "shared_artist"):
            assert not fails(conn, sql, (line, kind, reason, "proposed"))
            conn.execute("DELETE FROM credit_pick_proposals")


def test_decided_at_is_set_exactly_when_decided(conn: Conn) -> None:
    duet = Duet(conn)
    line = duet.duet_line(conn)
    with_decided = insert("decided_at")
    without = insert()

    assert fails(conn, with_decided, (line, "credit", "credit", "proposed", "2026-11-12"))
    assert fails(conn, without, (line, "credit", "credit", "applied"))
    for state in ("skipped", "applied", "picked"):
        assert not fails(conn, with_decided, (line, "credit", "credit", state, "2026-11-12"))
        conn.execute("DELETE FROM credit_pick_proposals")


def test_defaults(conn: Conn) -> None:
    duet = Duet(conn)
    line = duet.duet_line(conn)
    conn.execute(
        """INSERT INTO credit_pick_proposals (identity_id, kind, reason)
           VALUES (%s, 'tie', 'credit')""",
        (line,),
    )

    [row] = conn.execute(
        "SELECT state, candidates, proposed_at, decided_at FROM credit_pick_proposals"
    ).fetchall()
    assert (row["state"], row["candidates"], row["decided_at"]) == ("proposed", [], None)
    assert row["proposed_at"] is not None


def test_a_deleted_line_takes_its_proposal(conn: Conn) -> None:
    duet = Duet(conn)
    line = duet.duet_line(conn)
    seed.propose(conn, line, duet.duet)

    conn.execute("DELETE FROM play_events WHERE identity_id = %s", (line,))
    conn.execute("DELETE FROM matches WHERE identity_id = %s", (line,))
    conn.execute("DELETE FROM track_identities WHERE id = %s", (line,))

    assert seed.proposals(conn) == []


def test_a_deleted_match_or_performance_leaves_a_stale_proposal(conn: Conn) -> None:
    duet = Duet(conn)
    line = duet.duet_line(conn)
    seed.propose(conn, line, duet.duet)

    conn.execute("DELETE FROM matches WHERE identity_id = %s", (line,))
    conn.execute(
        "UPDATE library_files SET performance_id = NULL WHERE performance_id = %s", (duet.duet,)
    )
    conn.execute("DELETE FROM performances WHERE id = %s", (duet.duet,))

    [row] = seed.proposals(conn)
    assert (row["match_id"], row["performance_id"], row["state"]) == (None, None, "proposed")

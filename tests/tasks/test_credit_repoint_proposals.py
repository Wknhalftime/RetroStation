"""The Credit picks proposals (performances phase 3a PR 3, SPEC S8, D4, D8, D9; design C57 as
D4 amends it, M27, C64, C65; AUD-R039's 3a check), on PostgreSQL.

After PR 2's build has finished (its done-marker), a task asks ``choose_performance`` about every
matched line (D4: auto or manual matched, its best match with a file, that file's work, else its
recording's work, as the view finds it): the log name's **active** links against the work's
performances, the default being the performance of the master's file (D9; an untagged one
counts as crediting the work's artist, D8). It writes one row per line it proposes, to
``credit_pick_proposals``:

- ``credit``: the answer is a non-default performance (reason ``credit`` or ``shared_artist``)
  and the line names no version -- the performance, with it as the only candidate;
- ``version_line``: the same answer for a line whose version text ``read_log_version``
  recognises (2b's Unmapped page handles its version) -- no performance, the best non-default
  performances as candidates, for Lance's per-line pick (C65);
- ``tie``: two or more non-default performances tie (S6) -- no performance, the tied ones as
  candidates, listed with the version lines.

A line whose answer is the default, ``no_performer``, or the performance it already stores, gets
no row. Credits compare by merge-group primary; a credit held only by MBID finds its artist by
that MBID (D1).

A re-run keeps every decided row (``skipped``, ``applied``, ``picked``) as it is. An undecided
row whose answer (best match, kind, reason, performance, candidates) is unchanged keeps its id;
one whose answer changed is replaced by a new row with a new id, so an Apply naming the old id
finds it stale and never stores an answer Lance didn't see (C64); undecided rows no longer
proposed are dropped, and new lines are added. The task changes nothing that plays. It reports
on its own progress type, ``credit_pick_proposals``; it has no done-marker, so it can run again
(4.4).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.domain.library import PERFORMANCES_BUILT_SETTING
from backend.tasks import huey_app
from backend.tasks.credit_pick_tasks import propose_credit_picks_task
from tests.integration import credit_picks_seed as seed
from tests.integration.credit_picks_seed import Conn, Duet

pytestmark = pytest.mark.integration


@pytest.fixture
def conn(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Conn]:
    """The test's connection. The settings name the test database, so the task's own connections
    use it, and nothing it might queue leaves the test. The build counts as done."""
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setattr(huey_app.huey, "enqueue", lambda _task: None)
    get_settings.cache_clear()
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        seed.mark_built(connection)
        yield connection
    get_settings.cache_clear()


@pytest.fixture
def duet(conn: Conn) -> Duet:
    return Duet(conn)


def run(conn: Conn) -> dict[str, Any]:
    """One run of the task, with the test's work committed first."""
    conn.commit()
    result: dict[str, Any] = propose_credit_picks_task.call_local()
    conn.rollback()
    return result


def proposed(conn: Conn) -> dict[UUID, tuple[str, str, UUID | None, list[UUID], str]]:
    """line -> (kind, reason, performance, candidates, state) of every row."""
    return {
        UUID(str(r["identity_id"])): (
            r["kind"],
            r["reason"],
            None if r["performance_id"] is None else UUID(str(r["performance_id"])),
            [UUID(str(c)) for c in r["candidates"]],
            r["state"],
        )
        for r in seed.proposals(conn)
    }


# --- who is proposed ------------------------------------------------------------------------------


def test_a_duet_line_is_proposed(conn: Conn, duet: Duet) -> None:
    """The line matched to the solo master gets the duet; the row names its best match."""
    line = duet.duet_line(conn)

    result = run(conn)

    assert result["done"] is True
    assert proposed(conn) == {line: ("credit", "credit", duet.duet, [duet.duet], "proposed")}
    row = seed.proposal_of(conn, line)
    assert row is not None
    assert (row["match_id"], row["decided_at"]) == (seed.best_match(conn, line), None)


def test_solo_and_unlinked_lines_get_none(conn: Conn, duet: Duet) -> None:
    """The default answers a solo line; a log name with no link, or only suggested or rejected
    links, gets the default (C16)."""
    duet.solo_line(conn)
    seed.line(conn, "Who Says You Can't Go Home", duet.master)
    suggested = seed.log_name(
        conn, "BON JOVI/NETTLES", duet.bon_jovi, duet.nettles, state="suggested"
    )
    seed.line(conn, "Who Says You Can't Go Home", duet.master, name=suggested)
    rejected = seed.log_name(
        conn, "BON JOVI & NETTLES", duet.bon_jovi, duet.nettles, state="rejected"
    )
    seed.line(conn, "Who Says You Can't Go Home", duet.master, name=rejected)

    run(conn)

    assert proposed(conn) == {}


def test_only_active_links_count(conn: Conn, duet: Duet) -> None:
    """A duet name whose Nettles link is only suggested names Bon Jovi alone: a solo line."""
    name = seed.log_name(conn, "BON JOVI W/ J NETTLES", duet.bon_jovi)
    seed.link(conn, name, duet.nettles, state="suggested")
    seed.line(conn, "Who Says You Can't Go Home", duet.master, name=name)

    run(conn)

    assert proposed(conn) == {}


def test_every_matched_line(conn: Conn, duet: Duet) -> None:
    """D4: a line matched to the master and one matched to another copy are both proposed, and a
    manual match counts; a line in review, or matched to a file on no work, is not."""
    on_master = duet.duet_line(conn)
    on_copy = seed.line(conn, "Who Says You Can't Go Home", duet.duet_mp3, name=duet.duet_name)
    manual = seed.line(
        conn,
        "Who Says You Can't Go Home",
        duet.master,
        name=duet.duet_name,
        status="manual_matched",
    )
    seed.line(
        conn, "Who Says You Can't Go Home", duet.master, name=duet.duet_name, status="needs_review"
    )
    seed.line(conn, "Who Says You Can't Go Home", seed.copy(conn, None), name=duet.duet_name)

    run(conn)

    assert set(proposed(conn)) == {on_master, on_copy, manual}


def test_a_line_already_storing_its_answer_gets_none(conn: Conn, duet: Duet) -> None:
    line = duet.duet_line(conn)
    seed.store(conn, line, duet.duet)

    run(conn)

    assert proposed(conn) == {}


def test_a_work_with_no_performances_gets_none(conn: Conn) -> None:
    bon_jovi, nettles = seed.artist(conn, "Bon Jovi"), seed.artist(conn, "Jennifer Nettles")
    work = seed.work(conn, "Who Says You Can't Go Home", bon_jovi)
    master = seed.copy(conn, work)
    seed.master(conn, work, master)
    name = seed.log_name(conn, "BON JOVI/JENNIFER NETTLES", bon_jovi, nettles)
    seed.line(conn, "Who Says You Can't Go Home", master, name=name)

    run(conn)

    assert proposed(conn) == {}


def test_shared_artist(conn: Conn, duet: Duet) -> None:
    """M21: "JENNIFER NETTLES/KID ROCK" has no performance of its own; the duet shares one."""
    kid_rock = seed.artist(conn, "Kid Rock")
    name = seed.log_name(conn, "JENNIFER NETTLES/KID ROCK", duet.nettles, kid_rock)
    line = seed.line(conn, "Who Says You Can't Go Home", duet.master, name=name)

    run(conn)

    assert proposed(conn) == {line: ("credit", "shared_artist", duet.duet, [duet.duet], "proposed")}


def test_no_performer_gets_none(conn: Conn, duet: Duet) -> None:
    kid_rock = seed.artist(conn, "Kid Rock")
    seed.line(
        conn,
        "Who Says You Can't Go Home",
        duet.master,
        name=seed.log_name(conn, "KID ROCK", kid_rock),
    )

    run(conn)

    assert proposed(conn) == {}


def test_version_lines_listed_apart(conn: Conn, duet: Duet) -> None:
    """C65: a duet line naming a version gets no performance, the duet as its candidate; a solo
    line naming a version gets nothing (2b's page has it); title words aren't a version."""
    live = duet.duet_line(conn, "Who Says You Can't Go Home (Live)")
    duet.solo_line(conn, "Who Says You Can't Go Home (Live)")
    words = duet.duet_line(conn, "Who Says You Can't Go Home (I'm Special)")

    run(conn)

    assert proposed(conn) == {
        live: ("version_line", "credit", None, [duet.duet], "proposed"),
        words: ("credit", "credit", duet.duet, [duet.duet], "proposed"),
    }


def test_a_tie_is_listed_apart(conn: Conn, duet: Duet) -> None:
    """Two duet performances answer equally: no performance, both as candidates (D3)."""
    other_copy = seed.copy(conn, duet.work)
    other = seed.performance(
        conn,
        duet.work,
        [other_copy],
        credit=[duet.nettles, duet.bon_jovi],
        recording_mbid="r-duet-two",
    )
    line = duet.duet_line(conn)

    run(conn)

    [(kind, reason, performance, candidates, state)] = proposed(conn).values()
    assert (kind, reason, performance, state) == ("tie", "credit", None, "proposed")
    assert sorted(candidates) == sorted([duet.duet, other])
    assert list(proposed(conn)) == [line]


def test_merged_artists_compare_as_their_primary(conn: Conn, duet: Duet) -> None:
    """2a's Merge: the log name is linked to a stub merged into Nettles."""
    stub = seed.artist(conn, "Nettles, Jennifer")
    seed.merge(conn, stub, duet.nettles)
    name = seed.log_name(conn, "BON JOVI F/JENNIFER NETTLES", duet.bon_jovi, stub)
    line = seed.line(conn, "Who Says You Can't Go Home", duet.master, name=name)

    run(conn)

    assert proposed(conn) == {line: ("credit", "credit", duet.duet, [duet.duet], "proposed")}


def test_a_credit_by_mbid_finds_its_artist(conn: Conn) -> None:
    """D1: the duet credits Nettles by MBID alone; once an artist holds that MBID, the credit
    is hers. An MBID no artist holds is no one the line names."""
    bon_jovi = seed.artist(conn, "Bon Jovi")
    work = seed.work(conn, "Who Says You Can't Go Home", bon_jovi)
    master = seed.copy(conn, work)
    seed.performance(conn, work, [master], credit=[bon_jovi])
    seed.master(conn, work, master)
    held = seed.copy(conn, work)
    nettles_mbid = "0e000000-0000-4000-8000-00000000000e"
    duet = seed.performance(conn, work, [held], credit=[bon_jovi], by_mbid=[nettles_mbid])
    unknown = seed.performance(
        conn, work, [seed.copy(conn, work)], credit=[bon_jovi], by_mbid=[str(UUID(int=99))]
    )
    nettles = seed.artist(conn, "Jennifer Nettles", mbid=nettles_mbid)
    name = seed.log_name(conn, "BON JOVI/JENNIFER NETTLES", bon_jovi, nettles)
    line = seed.line(conn, "Who Says You Can't Go Home", master, name=name)

    run(conn)

    assert proposed(conn) == {line: ("credit", "credit", duet, [duet], "proposed")}
    assert unknown not in proposed(conn)[line][3]


def test_an_untagged_master_credits_its_works_artist(conn: Conn) -> None:
    """D8: the untagged master's performance counts as crediting Bon Jovi, the work's artist (a
    name-only row merged into the one the log names link to). It keeps the solo line; the duet
    line and the Nettles line get the duet."""
    bon_jovi, nettles = seed.artist(conn, "Bon Jovi"), seed.artist(conn, "Jennifer Nettles")
    name_only = seed.artist(conn, "Bon Jovi")
    seed.merge(conn, name_only, bon_jovi)
    work = seed.work(conn, "Who Says You Can't Go Home", name_only)
    master = seed.copy(conn, work)
    seed.performance(conn, work, [master])
    seed.master(conn, work, master)
    seed.performance(conn, work, [seed.copy(conn, work)], credit=[bon_jovi], recording_mbid="r1")
    duet = seed.performance(conn, work, [seed.copy(conn, work)], credit=[bon_jovi, nettles])

    def line_of(name: str, *artists: str) -> UUID:
        return seed.line(
            conn, "Who Says You Can't Go Home", master, name=seed.log_name(conn, name, *artists)
        )

    line_of("BON JOVI", bon_jovi)
    duet_line = line_of("BON JOVI/JENNIFER NETTLES", bon_jovi, nettles)
    nettles_line = line_of("JENNIFER NETTLES", nettles)

    run(conn)

    assert proposed(conn) == {
        duet_line: ("credit", "credit", duet, [duet], "proposed"),
        nettles_line: ("credit", "credit", duet, [duet], "proposed"),
    }


# --- re-runs, air play, the marker, progress ------------------------------------------------------


def test_rerun_keeps_decisions(conn: Conn, duet: Duet) -> None:
    """Decided rows stay as they are, even when no longer proposed. An undecided row keeps its
    id while its answer is the same; one whose answer changed (here its reason) is replaced by a
    new row (C64); one no longer proposed is dropped; a new line is added."""
    kid_rock = seed.artist(conn, "Kid Rock")
    decided_name = seed.log_name(conn, "BON JOVI & JENNIFER NETTLES", duet.bon_jovi, duet.nettles)
    applied, skipped = (
        seed.line(conn, "Who Says You Can't Go Home", duet.master, name=decided_name)
        for _ in range(2)
    )
    kept = duet.duet_line(conn)
    shared_name = seed.log_name(conn, "JENNIFER NETTLES/KID ROCK", duet.nettles, kid_rock)
    changed = seed.line(conn, "Who Says You Can't Go Home", duet.master, name=shared_name)
    dropped_name = seed.log_name(conn, "BON JOVI + JENNIFER NETTLES", duet.bon_jovi, duet.nettles)
    dropped = seed.line(conn, "Who Says You Can't Go Home", duet.master, name=dropped_name)
    run(conn)
    before = {r["identity_id"]: dict(r) for r in seed.proposals(conn)}
    conn.execute(
        """UPDATE credit_pick_proposals SET state = 'applied', decided_at = now()
           WHERE identity_id = %s""",
        (applied,),
    )
    conn.execute(
        """UPDATE credit_pick_proposals SET state = 'skipped', decided_at = now()
           WHERE identity_id = %s""",
        (skipped,),
    )
    decided = {r["identity_id"]: dict(r) for r in seed.proposals(conn)}
    conn.execute(
        "DELETE FROM log_name_links WHERE broadcast_artist_id = %s AND artist_id = %s",
        (shared_name, kid_rock),
    )
    conn.execute(
        "UPDATE log_name_links SET state = 'rejected' WHERE broadcast_artist_id = %s",
        (dropped_name,),
    )
    conn.execute(
        "UPDATE log_name_links SET state = 'rejected' WHERE broadcast_artist_id = %s",
        (decided_name,),
    )
    added = seed.line(
        conn,
        "Who Says You Can't Go Home",
        duet.master,
        name=seed.log_name(conn, "JENNIFER NETTLES W/ BON JOVI", duet.nettles, duet.bon_jovi),
    )

    run(conn)

    after = {r["identity_id"]: dict(r) for r in seed.proposals(conn)}
    assert dropped in before
    assert set(after) == {applied, skipped, kept, changed, added}
    assert after[applied] == decided[applied]
    assert after[skipped] == decided[skipped]
    assert after[kept] == before[kept]
    assert after[changed]["id"] != before[changed]["id"]
    assert (after[changed]["reason"], after[changed]["state"]) == ("credit", "proposed")
    assert after[added]["state"] == "proposed"


def test_a_changed_answer_is_a_new_row(conn: Conn, duet: Duet) -> None:
    """C64: a re-run never changes what a listed row would store under the same id. A new
    performance answering the line better, or a new best match, gives the line a new row; the
    old id is gone, so an Apply that names it finds it stale."""
    nettles_line = seed.line(
        conn,
        "Who Says You Can't Go Home",
        duet.master,
        name=seed.log_name(conn, "JENNIFER NETTLES", duet.nettles),
    )
    rematched = duet.duet_line(conn)
    run(conn)
    first = {r["identity_id"]: r["id"] for r in seed.proposals(conn)}
    her_copy = seed.copy(conn, duet.work)
    hers = seed.performance(conn, duet.work, [her_copy], credit=[duet.nettles], recording_mbid="n1")
    seed.also_match(conn, rematched, duet.duet_mp3, confidence=0.95)

    run(conn)

    rows = {UUID(str(r["identity_id"])): r for r in seed.proposals(conn)}
    assert UUID(str(rows[nettles_line]["performance_id"])) == hers
    assert rows[nettles_line]["id"] != first[nettles_line]
    assert UUID(str(rows[rematched]["match_id"])) == seed.best_match(conn, rematched)
    assert rows[rematched]["id"] != first[rematched]
    assert {r["id"] for r in rows.values()}.isdisjoint(first.values())


def test_it_changes_nothing_that_plays(conn: Conn, duet: Duet) -> None:
    lines = [duet.duet_line(conn), duet.solo_line(conn), duet.duet_line(conn, "Home (Live)")]
    conn.execute(
        "UPDATE performances SET preferred_copy_id = %s WHERE id = %s",
        (duet.duet_flac, duet.duet),
    )
    plays_before = seed.every_play(conn)
    stored_before = [seed.stored(conn, line) for line in lines]
    preferred_before = seed.preferred_of(conn, duet.duet)

    run(conn)

    assert seed.every_play(conn) == plays_before
    assert [seed.stored(conn, line) for line in lines] == stored_before
    assert seed.preferred_of(conn, duet.duet) == preferred_before
    assert seed.preferred_of(conn, duet.solo) is None


def test_it_waits_for_the_build(conn: Conn, duet: Duet) -> None:
    conn.execute("DELETE FROM user_settings WHERE key = %s", (PERFORMANCES_BUILT_SETTING,))
    duet.duet_line(conn)

    result = run(conn)

    assert result["done"] is False
    assert proposed(conn) == {}


def test_the_run_reports_on_its_own_progress_type(conn: Conn, duet: Duet) -> None:
    duet.duet_line(conn)

    run(conn)

    row = conn.execute(
        "SELECT status FROM progress_tracking WHERE task_type = 'credit_pick_proposals'"
    ).fetchone()
    assert row is not None and row["status"] == "completed"

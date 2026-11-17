"""The guarded work merge (artist model phase 3b PR 3; SPEC S3, S4, D2-D4, D13, D14, D23, E2, E4,
E7, E8; design C58, C61, C65, C71, C72, C74, M21; AUD-R079).

One guarded merge is the only way two works merge (D3). Inside one transaction (a savepoint on
the caller's connection) the guard reads the group's (station, line) results through the
air-play view, calls ``merge_works``, reads them again, and undoes the merge when a change is
not approved. ``merge_works`` itself keeps every line where it was, where it can:

- **Pins (S3, C72).** A line that reached its master step, and whose master's file changes,
  is pinned: its best match stores the file that step played (``matches.pinned_file_id``),
  through ``SongMatchService``. The view reads the pin just before the master (S12). A line
  whose file stays, or that only an override or entry plays, is not pinned.
- **Cover lines (S4, C65, M21).** When that line's log name's active links pick a non-default
  performance of the merged work (``choose_performance``) holding the file it played, the line
  stores that performance instead, with that file as its preferred copy, so a later Set master
  keeps it on its own act (C71). A line with no linked artist is pinned.
- **What a pin can't hold is a flag (C74, D2).** An override or version entry reaching a line,
  or a stored line handed off or released (3a's D5) whose file changes, is a change the guard
  finds; the merge is undone and the changes returned, unless each was approved as its
  (station, line, new file) (Q8). A stored line is never pinned (D2's road not taken).

A merge that goes through is logged (the merge log row with its reason and approvals, and a
``works_merged`` event), and each merged-away id redirects to the merged work; a redirect
always names a live work, so a chain needs one lookup. A merge that fails is undone alone, and
logged as failed. A flag is no failure: nothing is logged. A late MusicBrainz work link (E4)
runs the same guard with reason "musicbrainz work link" and no approvals.

Pins after the merge (D4, D14, D23, E7): the fold re-points a pin to the found copy on the same
work, and releases it (logged, ``pins_released``) when the found copy is on another work;
deleting a missing file (Missing files' delete, the purge) releases its pins first, logged; a
pinned file that split, reassign or enrichment's move takes to another work releases its pins,
logged. A path that forgets fails at the foreign key. A released line takes the normal path.

Every seed stays uncommitted: the guard runs on the test's own connection.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from structlog.testing import capture_logs

from backend.domain.library import MissingFileMove, MissingFileSelection
from backend.domain.work_merges import (
    MergeApproval,
    MergeDone,
    MergeReason,
    MergeRefused,
    WorkMergeFailedError,
)
from backend.services.master_choice_service import MasterChoiceRepos, choose_master
from backend.services.missing_file_reconciliation_service import (
    apply_missing_file_move,
    delete_missing_files,
    purge_unmatched_missing,
)
from backend.services.repository_factory import (
    RepositoryFactory,
    reconciliation_repos,
    work_curation_repos,
)
from backend.services.work_curation_service import reassign_file, split_file_off
from backend.services.work_move_service import WorkMoveRepos, move_file_to_work
from tests.integration import credit_picks_seed as cp
from tests.integration import work_merge_seed as seed
from tests.integration.credit_picks_seed import Conn
from tests.integration.work_merge_seed import Cover, TwoWorks

pytestmark = pytest.mark.integration

PINS_RELEASED = "pins_released"


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def released(logs: list[dict[str, object]], file_id: UUID) -> list[dict[str, object]]:
    """The ``pins_released`` events naming *file_id*."""
    return [e for e in logs if e["event"] == PINS_RELEASED and e.get("file_id") == str(file_id)]


# --- S3: pins -------------------------------------------------------------------------------------


def test_every_line_whose_file_would_change_is_pinned(conn: Conn) -> None:
    """The merged work's master is the target's flac (D8), so both lines on the source's master
    would move to it: each is pinned to the file it played, and plays it from the pin."""
    world = TwoWorks(conn)
    at = seed.station(conn)
    lines = [seed.heard(conn, world.source_master, at, title="Always") for _ in range(2)]
    assert all(seed.playing(conn, line) == {(world.source_master, "master")} for line in lines)

    done = seed.merged(conn, world.target, [world.source])

    assert done.changes == ()
    assert seed.master_of(conn, world.target) == (world.target_master, "auto")
    for line in lines:
        assert seed.pin_of(conn, line) == world.source_master
        assert seed.playing(conn, line) == {(world.source_master, "pin")}
        assert seed.stored_of(conn, line) is None


@pytest.mark.parametrize("whose", ["the targets line", "a line only an override plays"])
def test_a_line_whose_file_stays_is_not_pinned(conn: Conn, whose: str) -> None:
    """The target's line keeps its master; a source line heard only where the source's AC
    override plays keeps that override, which moves with its work. Neither is pinned."""
    world = TwoWorks(conn)
    if whose == "the targets line":
        at, played = seed.station(conn), world.target_master
        line = seed.heard(conn, world.target_master, at, title="Always")
    else:
        at, played = seed.station(conn, "AC"), world.source_file(conn)
        cp.override(conn, world.source, "AC", played)
        line = seed.heard(conn, world.source_master, at, title="Always")
    before = seed.playing(conn, line)
    assert {file for file, _ in before} == {played}

    seed.merged(conn, world.target, [world.source])

    assert seed.pin_of(conn, line) is None
    assert seed.playing(conn, line) == before


def test_two_manual_masters_pin_the_other_works_lines(conn: Conn) -> None:
    """E1's last clause: the target's hand-picked master has more plays and stays; the
    source's lines, on the source's hand-picked master, are pinned to it."""
    world = TwoWorks(conn)
    seed.master(conn, world.target, world.target_master, "manual")
    seed.master(conn, world.source, world.source_master, "manual")
    at = seed.station(conn)
    seed.heard(conn, world.target_master, at, title="Always", times=3)
    source_line = seed.heard(conn, world.source_master, at, title="Always")

    seed.merged(conn, world.target, [world.source])

    assert seed.master_of(conn, world.target) == (world.target_master, "manual")
    assert seed.pin_of(conn, source_line) == world.source_master
    assert seed.playing(conn, source_line) == {(world.source_master, "pin")}


# --- S3, D3, C74: the guard -----------------------------------------------------------------------


def flagged(conn: Conn) -> tuple[TwoWorks, UUID, UUID, UUID]:
    """TwoWorks, the source with an AC override (an AC edit), and a target line heard at an AC
    station: the merge brings the override onto it (C74). (world, station, line, AC edit)"""
    world = TwoWorks(conn)
    ac_edit = world.source_file(conn)
    cp.override(conn, world.source, "AC", ac_edit)
    at = seed.station(conn, "AC")
    line = seed.heard(conn, world.target_master, at, title="Always")
    return world, at, line, ac_edit


def test_flag_blocks_unapproved(conn: Conn) -> None:
    """The guard finds the change, undoes the merge and returns it; nothing is written, not
    even a log row."""
    world, at, line, ac_edit = flagged(conn)
    before = seed.snapshot(conn)

    outcome = seed.refused(conn, world.target, [world.source])

    assert seed.change_keys(outcome.changes) == {(at, line, world.target_master, ac_edit)}
    assert seed.snapshot(conn) == before
    assert seed.playing(conn, line) == {(world.target_master, "master")}
    assert seed.redirects(conn, [world.source]) == {}
    assert seed.merges_of(conn, world.target) == []


def test_an_approved_flag_merges(conn: Conn) -> None:
    """Approved as its (station, line, new file), the change goes through: the line plays the
    override, and the log keeps the approval."""
    world, at, line, ac_edit = flagged(conn)
    approvals = seed.approving(seed.refused(conn, world.target, [world.source]).changes)

    done = seed.merged(conn, world.target, [world.source], approvals=approvals)

    assert seed.change_keys(done.changes) == {(at, line, world.target_master, ac_edit)}
    assert seed.playing(conn, line) == {(ac_edit, "override")}
    assert not seed.work_exists(conn, world.source)
    [logged] = seed.merges_of(conn, world.target)
    assert logged["approvals"] == [
        {"station_id": str(at), "identity_id": str(line), "file_id": str(ac_edit)}
    ]


@pytest.mark.parametrize("approved", ["one of two changes", "another file"])
def test_an_approval_covers_only_its_change(conn: Conn, approved: str) -> None:
    """Two changes: the AC override reaches one target line, the source's version entry for
    "live" another. Approving one, or both with another new file, is refused again, listing
    every change; nothing is written."""
    world, at, line, ac_edit = flagged(conn)
    live = world.source_file(conn)
    cp.version_entry(conn, world.source, "live", live)
    live_line = seed.heard(conn, world.target_master, seed.station(conn), title="Always (Live)")
    changes = seed.refused(conn, world.target, [world.source]).changes
    if approved == "one of two changes":
        approvals = seed.approving(c for c in changes if c.identity_id == line)
    else:
        approvals = [MergeApproval(c.station_id, c.identity_id, uuid4()) for c in changes]
    before = seed.snapshot(conn)

    outcome = seed.refused(conn, world.target, [world.source], approvals=approvals)

    assert {c.identity_id for c in outcome.changes} == {line, live_line}
    assert seed.change_keys(outcome.changes) == seed.change_keys(changes)
    assert seed.snapshot(conn) == before


def test_a_failed_merge_rolls_back_that_group_alone(conn: Conn) -> None:
    """Two groups on one connection; the second fails partway (a trigger planted by the test
    refuses deleting its source work). The first stays merged; the second is undone as a whole,
    raises, and is logged as failed with its error."""
    first, second = TwoWorks(conn), TwoWorks(conn, "Bed Of Roses")
    at = seed.station(conn)
    second_line = seed.heard(conn, second.source_master, at, title="Bed Of Roses")
    conn.execute(
        """CREATE FUNCTION refuse_one_work_delete() RETURNS trigger LANGUAGE plpgsql
           AS $$ BEGIN RAISE EXCEPTION 'work delete refused by the test'; END $$"""
    )
    conn.execute(
        sql.SQL(
            """CREATE TRIGGER refuse_one_work_delete BEFORE DELETE ON works FOR EACH ROW
               WHEN (OLD.id = {}) EXECUTE FUNCTION refuse_one_work_delete()"""
        ).format(sql.Literal(second.source))
    )
    try:
        seed.merged(conn, first.target, [first.source])
        before = seed.snapshot(conn)

        with pytest.raises(WorkMergeFailedError):
            seed.merge(conn, second.target, [second.source])

        assert seed.snapshot(conn) == before
        assert seed.pin_of(conn, second_line) is None
        assert not seed.work_exists(conn, first.source)
        assert seed.redirects(conn, [first.source, second.source]) == {first.source: first.target}
        [failed] = seed.merges_of(conn, second.target)
        assert failed["status"] == "failed"
        assert "work delete refused by the test" in failed["error"]
        assert [m["status"] for m in seed.merges_of(conn, first.target)] == ["merged"]
    finally:
        conn.execute("DROP TRIGGER IF EXISTS refuse_one_work_delete ON works")
        conn.execute("DROP FUNCTION IF EXISTS refuse_one_work_delete()")


# --- D2, E8: stored lines -------------------------------------------------------------------------


@pytest.mark.parametrize("settled", ["handed off", "released"])
def test_stored_line_change_flags(conn: Conn, settled: str) -> None:
    """A source line stores a non-default performance (Bon Jovi with Jennifer Nettles) whose
    one copy shares a recording MBID with the target's master. Merged, that copy joins the
    target's performance, so the line is handed off to it, the default, and would play the
    master; with a second copy of another recording the performance's files land apart and the
    line is released to the master. Either way its file changes: a flag, never a pin."""
    world = TwoWorks(conn)
    recording = str(uuid4())
    conn.execute(
        "UPDATE library_files SET recording_mbid = %s WHERE id = %s",
        (recording, world.target_master),
    )
    conn.execute(
        "UPDATE performances SET recording_mbid = %s WHERE id = %s",
        (recording, world.target_performance),
    )
    nettles = cp.artist(conn, "Jennifer Nettles")
    duet_copy = cp.copy(conn, world.source, title="Always", recording_mbid=recording)
    duet_files = [duet_copy]
    if settled == "released":
        duet_files.append(cp.copy(conn, world.source, title="Always", recording_mbid=str(uuid4())))
    duet = cp.performance(
        conn, world.source, duet_files, credit=[world.act, nettles], preferred=duet_copy
    )
    at = seed.station(conn)
    line = seed.heard(conn, world.source_master, at, title="Always")
    cp.store(conn, line, duet)
    assert seed.playing(conn, line) == {(duet_copy, "performance")}
    before = seed.snapshot(conn)

    outcome = seed.refused(conn, world.target, [world.source])

    assert seed.change_keys(outcome.changes) == {(at, line, duet_copy, world.target_master)}
    assert seed.snapshot(conn) == before


# --- S4, C65: cover lines keep their act ----------------------------------------------------------


def test_cover_lines_keep_their_act(conn: Conn) -> None:
    """LaBelle's flac is the merged master (D8). The Aguilera line would move to it; its links
    pick Aguilera's performance of the merged work, which holds the file it played, so the line
    stores that performance, its preferred copy that file. Nothing that plays changes."""
    world = Cover(conn)
    at = seed.station(conn)
    labelle_line, aguilera_line = world.labelle_line(conn, at), world.aguilera_line(conn, at)

    done = seed.merged(conn, world.target, [world.source])

    assert done.changes == ()
    assert seed.master_of(conn, world.target) == (world.labelle_master, "auto")
    aguilera = seed.performance_of(conn, world.aguilera_master)
    assert aguilera is not None
    assert aguilera != seed.performance_of(conn, world.labelle_master)
    assert seed.stored_of(conn, aguilera_line) == aguilera
    assert cp.preferred_of(conn, aguilera) == world.aguilera_master
    assert seed.pin_of(conn, aguilera_line) is None
    assert seed.playing(conn, aguilera_line) == {(world.aguilera_master, "performance")}
    assert seed.playing(conn, labelle_line) == {(world.labelle_master, "master")}
    assert seed.pin_of(conn, labelle_line) is None


def test_set_master_to_the_other_act_keeps_the_line(conn: Conn) -> None:
    """After the merge, Set master picks another LaBelle copy (an mp3, so the merge's re-pick
    keeps the flac): LaBelle's line moves to it (C71), the Aguilera line stays on Aguilera's
    copy."""
    world = Cover(conn)
    second_labelle = world.copy_of(conn, world.labelle, world.target)
    at = seed.station(conn)
    labelle_line, aguilera_line = world.labelle_line(conn, at), world.aguilera_line(conn, at)
    seed.merged(conn, world.target, [world.source])
    repos = RepositoryFactory(conn)

    choose_master(
        world.target,
        second_labelle,
        MasterChoiceRepos(
            move=WorkMoveRepos(
                files=repos.library_files,
                works=repos.works,
                song_masters=repos.song_masters,
                matches=repos.matches,
                performances=repos.performances,
            ),
            recordings=repos.recordings,
        ),
    )

    assert seed.playing(conn, labelle_line) == {(second_labelle, "master")}
    assert seed.playing(conn, aguilera_line) == {(world.aguilera_master, "performance")}


@pytest.mark.parametrize("reaches", ["format override", "version entry"])
def test_an_override_crossing_acts_flags(conn: Conn, reaches: str) -> None:
    """Aguilera's work holds an AC edit (a format override) or a live take (the version entry
    for "live"); merged, it would reach a LaBelle line, whose stored nothing can hold it (the
    view skips a default's stored performance, C71): a flag (C65, C74)."""
    world = Cover(conn)
    aguilera_take = world.copy_of(conn, world.aguilera, world.source)
    if reaches == "format override":
        cp.override(conn, world.source, "AC", aguilera_take)
        line = world.labelle_line(conn, at := seed.station(conn, "AC"))
    else:
        cp.version_entry(conn, world.source, "live", aguilera_take)
        line = world.labelle_line(conn, at := seed.station(conn), "Lady Marmalade (Live)")
    before = seed.snapshot(conn)

    outcome = seed.refused(conn, world.target, [world.source])

    assert seed.change_keys(outcome.changes) == {(at, line, world.labelle_master, aguilera_take)}
    assert seed.snapshot(conn) == before


def test_a_line_with_no_linked_artist_is_pinned(conn: Conn) -> None:
    """An Aguilera line whose log name links no artist gets the default from its links (C16),
    so it is pinned to the file it played instead."""
    world = Cover(conn)
    at = seed.station(conn)
    line = world.aguilera_line(conn, at, linked=False)

    seed.merged(conn, world.target, [world.source])

    assert seed.stored_of(conn, line) is None
    assert seed.pin_of(conn, line) == world.aguilera_master
    assert seed.playing(conn, line) == {(world.aguilera_master, "pin")}


# --- S3: the log and redirects --------------------------------------------------------------------


def test_each_merge_is_logged_with_its_reason(conn: Conn) -> None:
    world = TwoWorks(conn)

    with capture_logs() as logs:
        done = seed.merged(conn, world.target, [world.source])

    [logged] = seed.merges_of(conn, world.target)
    assert logged["id"] == done.merge_id
    assert logged["source_ids"] == [world.source]
    assert (logged["reason"], logged["status"], logged["error"]) == ("library", "merged", None)
    assert logged["approvals"] == []
    [event] = [e for e in logs if e["event"] == "works_merged"]
    assert event["merge_id"] == str(done.merge_id)
    assert event["target_id"] == world.target
    assert event["source_ids"] == [world.source]
    assert event["reason"] == "library"


def test_merged_ids_redirect(conn: Conn) -> None:
    """Every merged-away id redirects to the merged work, under the merge that took it."""
    world = TwoWorks(conn)
    third = cp.work(conn, "Always", world.act)
    cp.copy(conn, third, title="Always")

    done = seed.merged(conn, world.target, [world.source, third])

    assert seed.redirects(conn, [world.source, third, world.target]) == {
        world.source: world.target,
        third: world.target,
    }
    rows = conn.execute(
        "SELECT merge_id FROM work_redirects WHERE old_id = ANY(%s)", ([world.source, third],)
    ).fetchall()
    assert {r["merge_id"] for r in rows} == {done.merge_id}


def test_a_chain_of_redirects_resolves(conn: Conn) -> None:
    """A merged into B, then B into C: both redirect to C, so one lookup always reaches a live
    work."""
    act = cp.artist(conn, "Bon Jovi")
    a, b, c = (cp.work(conn, "Always", act) for _ in range(3))
    for work in (a, b, c):
        cp.copy(conn, work, title="Always")

    seed.merged(conn, b, [a])
    seed.merged(conn, c, [b])

    assert seed.redirects(conn, [a, b, c]) == {a: c, b: c}


# --- E2: a pinned copy goes missing ---------------------------------------------------------------


def test_pinned_copy_missing(conn: Conn) -> None:
    """The view reports the pinned copy as unavailable and never falls back to the master
    (C61, today's rule)."""
    world = TwoWorks(conn)
    line = seed.heard(conn, world.source_master, seed.station(conn), title="Always")
    seed.merged(conn, world.target, [world.source])

    seed.missing(conn, world.source_master)

    assert seed.playing(conn, line) == {(world.source_master, "pin")}
    assert seed.statuses(conn, line) == {"missing"}


# --- E4: a late MusicBrainz work link -------------------------------------------------------------


@pytest.mark.parametrize("flag", [False, True], ids=["unflagged-merges", "flagged-waits"])
def test_late_work_link(conn: Conn, flag: bool) -> None:
    """Phase 4's work link runs the same guard, reason "musicbrainz work link", pinning first,
    with no approvals. Flagged, it doesn't merge (the merge list shows it, PR 4)."""
    if flag:
        world, _, line, _ = flagged(conn)
    else:
        world = TwoWorks(conn)
        line = seed.heard(conn, world.source_master, seed.station(conn), title="Always")

    outcome = seed.merge(conn, world.target, [world.source], reason=MergeReason.WORK_LINK)

    if flag:
        assert isinstance(outcome, MergeRefused)
        assert seed.work_exists(conn, world.source)
        assert seed.pin_of(conn, line) is None
        assert seed.merges_of(conn, world.target) == []
    else:
        assert isinstance(outcome, MergeDone)
        assert seed.pin_of(conn, line) == world.source_master
        [logged] = seed.merges_of(conn, world.target)
        assert logged["reason"] == "musicbrainz work link"


# --- E7, D4, D23, D14: a pinned file folded, deleted or moved -------------------------------------


def pinned(conn: Conn) -> tuple[TwoWorks, UUID]:
    """TwoWorks merged, with a source line matched to the source's other copy and pinned to the
    source's master (which its master step played). (world, line)"""
    world = TwoWorks(conn)
    line = seed.heard(conn, world.source_copy, seed.station(conn), title="Always")
    assert seed.playing(conn, line) == {(world.source_master, "master")}
    seed.merged(conn, world.target, [world.source])
    assert seed.pin_of(conn, line) == world.source_master
    return world, line


def fold(conn: Conn, missing_id: UUID, successor_id: UUID) -> None:
    """Missing files' fold of the missing row into its found copy."""
    repos = RepositoryFactory(conn)
    move = MissingFileMove(
        missing_id=missing_id,
        missing_path=cp_path(conn, missing_id),
        missing_work_id=seed.work_of(conn, missing_id),
        successor_id=successor_id,
        successor_path=cp_path(conn, successor_id),
        successor_work_id=seed.work_of(conn, successor_id),
    )
    assert apply_missing_file_move(move, reconciliation_repos(repos))


def cp_path(conn: Conn, file_id: UUID) -> str:
    row = conn.execute("SELECT file_path FROM library_files WHERE id = %s", (file_id,)).fetchone()
    assert row is not None
    return str(row["file_path"])


@pytest.mark.parametrize(
    "what", ["fold on the same work", "fold onto another work", "missing file delete", "purge"]
)
def test_pinned_file_folded_or_deleted(conn: Conn, what: str) -> None:
    """E7 as D23 amends it: the fold re-points the pin to the found copy on the same work; on
    another work it releases the pin, logged, and the line takes the normal path (the master).
    Deleting the missing file, by Missing files' delete or the purge, releases the pin first,
    logged."""
    world, line = pinned(conn)
    seed.missing(conn, world.source_master)
    repos = RepositoryFactory(conn)

    with capture_logs() as logs:
        if what == "fold on the same work":
            found = cp.copy(conn, world.target, title="Always")
            fold(conn, world.source_master, found)
        elif what == "fold onto another work":
            found = cp.copy(conn, cp.work(conn, "Always", world.act), title="Always")
            fold(conn, world.source_master, found)
        elif what == "missing file delete":
            delete_missing_files(
                MissingFileSelection(ids=(world.source_master,)),
                reconciliation_repos(repos),
                repos.broadcast_identities,
            )
        else:
            root = cp_path(conn, world.source_master).rsplit("/", 1)[0]
            purge_unmatched_missing(root, reconciliation_repos(repos), repos.broadcast_identities)

    if what == "fold on the same work":
        assert seed.pin_of(conn, line) == found
        assert seed.playing(conn, line) == {(found, "pin")}
        assert released(logs, world.source_master) == []
    else:
        assert seed.pin_of(conn, line) is None
        assert seed.playing(conn, line) == {(world.target_master, "master")}
        [event] = released(logs, world.source_master)
        assert event["lines"] == 1
    assert (
        conn.execute("SELECT 1 FROM library_files WHERE id = %s", (world.source_master,)).fetchone()
        is None
    )


def test_a_forgotten_pin_fails_at_the_fk(conn: Conn) -> None:
    """D4: no ON DELETE action, so a path that deletes a pinned file without releasing its pins
    fails loudly instead of dropping them unlogged."""
    world, _ = pinned(conn)

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute("DELETE FROM library_files WHERE id = %s", (world.source_master,))


@pytest.mark.parametrize("path", ["split", "reassign", "enrichments move"])
def test_a_pinned_file_moved_to_another_work_releases_its_pin(conn: Conn, path: str) -> None:
    """D14: a pin stands in for its line's work's master only, so a pinned file that moves to
    another work releases its pins, logged, and the line takes the normal path."""
    world, line = pinned(conn)
    other = cp.work(conn, "Always", world.act)
    cp.copy(conn, other, title="Always")
    repos = RepositoryFactory(conn)

    with capture_logs() as logs:
        if path == "split":
            split_file_off(world.target, world.source_master, work_curation_repos(repos))
        elif path == "reassign":
            reassign_file(world.source_master, other, work_curation_repos(repos))
        else:
            moving = repos.library_files.get_by_id(world.source_master)
            assert moving is not None
            move_file_to_work(
                moving,
                other,
                WorkMoveRepos(
                    files=repos.library_files,
                    works=repos.works,
                    song_masters=repos.song_masters,
                    matches=repos.matches,
                    performances=repos.performances,
                ),
            )

    assert seed.work_of(conn, world.source_master) != world.target
    assert seed.pin_of(conn, line) is None
    assert seed.playing(conn, line) == {(world.target_master, "master")}
    [event] = released(logs, world.source_master)
    assert event["lines"] == 1

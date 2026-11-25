"""The cache fill's work links (MusicBrainz cache fill, phase 4.1 PR 3; SPEC S5; D1, D13; R1, R7;
the coordinator's readings R3-a to R3-h; R3-i and R3-j, as Lance ruled them in D15 and D16;
AUD-R092), on PostgreSQL through ``cache_fill_rig``.

- **The tier** (R3-a, D13): ``work_links``, capped at 3,000 calls a run, after confirmations and
  before the tail. It fills no names, so it queues no re-check (R3-h).
- **The work list** (R3-b): a performance whose recording has no known work link: its work isn't
  a MusicBrainz work, no live ``recording:`` row names a work for it, and no live
  ``recording-work:`` row exists for it. Its release is one a present file of the recording names.
  A release with a live ``release-missing:`` marker is left out, and so is a release whose live
  ``release:`` row doesn't carry the recording (R3-i, D15: one MusicBrainz has since merged).
- **The order** (R3-c): the release covering the most of those recordings first, ties by its
  MBID; a later release counts only the recordings still left, so some releases are never asked.
- **The call** (R3-d): its keys are the ``recording-work:`` keys of the recordings it's planned
  for, so a release is asked though some of its tracks are already linked. Each batch plans
  again from what is in the cache, so the rows a lookup wrote are linked for the next batch.
- **The rows** (R3-e): every track of a looked-up release, on every medium, gets a
  ``recording-work:{rid}`` row: the work of its recording's first ``performance`` relation naming
  a work with an id and a title, or none. ``lookup_release`` caches the release itself, and a
  cached release costs no request.
- **A 404** (R3-f) writes the marker and nothing else; it's an answer, not a failure.
- **Through 2b's budget** (AUD-R093): a refused release is one request, followed by the pause,
  and waits for the next run; a 400 is skipped for the run; five refusals in a row end it.
- **When another release is asked** (R3-j, D16): after a 404 or a release that doesn't carry the
  recording, the next time the work list is planned. A batch's plan is fixed, so that's the next
  batch if the run goes on, otherwise the next run. A refused release's recordings are the run's
  missed keys, so they wait for the next run, through the release the order picks.

The rows reach the merge list on its next load (D1); nothing merges until Lance's Apply.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.enums import FillTier
from backend.domain.library import WORK_ARTISTS_CREDITED_SETTING
from backend.domain.work_merge_planner import MergeJoin
from backend.services.merge_plan_service import plan
from backend.services.repository_factory import RepositoryFactory
from tests.integration.cache_fill_rig import (
    Asked,
    Conn,
    Fill,
    cache_entry,
    cache_keys,
    cached,
    confirmation,
    library_file,
    limits,
    line,
    log_name,
    nonzero,
)
from tests.integration.credit_picks_seed import set_marker
from tests.integration.work_link_seed import (
    UNLINKED,
    file_on,
    linked,
    performance,
    performs,
    recording_mbid,
    release,
    release_mbid,
    song,
    track,
    work,
    work_key,
    work_mbid,
)

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 23, 9, tzinfo=UTC)


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


@pytest.fixture
def fill(conn: Conn, migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Fill]:
    rig = Fill(conn, migrated_db, monkeypatch)
    yield rig
    rig.close()


def asked_releases(fill: Fill) -> list[str]:
    """The releases looked up, in order."""
    return [a.mbid for a in fill.server.asked("release")]


def answers(fill: Fill, *mbids: str) -> None:
    """MusicBrainz knows each release in *mbids*, its one track the recording of the same
    number, performing no work."""
    for mbid in mbids:
        fill.server.releases[mbid] = release(mbid, [track(recording_mbid(int(mbid[-12:], 16)))])


def stored_as(conn: Conn, key: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT entity_type, entity_mbid FROM mb_cache WHERE cache_key = %s", (key,)
    ).fetchone()
    assert row is not None, key
    return str(row["entity_type"]), str(row["entity_mbid"])


def again(fill: Fill) -> list[str]:
    """Run once more; the releases that run looked up."""
    fill.server.requests.clear()
    fill.run()
    return asked_releases(fill)


# --- the tier ------------------------------------------------------------------------------------


def test_a_release_lookup_writes_every_tracks_work_link(conn: Conn, fill: Fill) -> None:
    """S5, R3-e: the tier looks up the release a present file of the song names, and writes a
    ``recording-work:`` row for every track on it, on every medium. A track's work is its
    recording's first ``performance`` relation naming a work with an id and a title; a track
    with none gets a row naming none. ``lookup_release`` caches the release itself."""
    one = release_mbid(1)
    song(conn, "Battery", recording_mbid(1), one)
    fill.server.releases[one] = release(
        one,
        [track(recording_mbid(1), performs(work_mbid(1), "Battery")), track(recording_mbid(2))],
        [
            track(
                recording_mbid(3),
                {"type": "medley", "work": {"id": work_mbid(7), "title": "A Medley"}},
                {"type": "performance", "work": {"id": work_mbid(8)}},
                performs(work_mbid(3), "Orion"),
                performs(work_mbid(9), "Later"),
            )
        ],
    )

    fill.run()

    assert fill.server.requests == [Asked("release", mbid=one)]
    assert cache_keys(conn, "recording-work:") == {
        work_key(recording_mbid(1)),
        work_key(recording_mbid(2)),
        work_key(recording_mbid(3)),
    }
    assert cache_entry(conn, work_key(recording_mbid(1))) == linked(work_mbid(1), "Battery")
    assert cache_entry(conn, work_key(recording_mbid(2))) == UNLINKED
    assert cache_entry(conn, work_key(recording_mbid(3))) == linked(work_mbid(3), "Orion")
    assert stored_as(conn, work_key(recording_mbid(1))) == ("recording-work", recording_mbid(1))
    assert cache_entry(conn, f"release:{one}") == fill.server.releases[one]


def test_the_tier_and_its_row(conn: Conn, fill: Fill) -> None:
    """R3-a, D4: the tier's row reads like any other's, under D5's cap of 3,000 (AUD-R093's
    report included), and the tier fills no names, so it queues no re-check (R3-h). Its rows
    are live: the next run asks nothing."""
    song(conn, "Battery", recording_mbid(1), release_mbid(1))
    answers(fill, release_mbid(1))

    fill.run()

    last = fill.last()
    assert (last["tier"], last["cap"]) == (FillTier.WORK_LINKS.value, 3000)
    assert last["detail"] == "work_links, 1 of 1 (cap 3,000)"
    assert nonzero(last["calls"]) == {"work_links": 1}
    assert (last["requests"], last["ended"]) == (1, "done")
    assert fill.rechecks() == []
    assert again(fill) == []


def test_the_tier_comes_after_confirmations_and_before_the_tail(conn: Conn, fill: Fill) -> None:
    """D13, "Before the tail": payoff, C68, C18, confirmations, work links, tail, then the alias
    step."""
    heart = log_name(conn, "HEART")
    line(conn, heart, "Barracuda", plays=5)  # payoff: a present file holds it
    line(conn, heart, "Magic Man", plays=50)  # the tail: no file holds it
    library_file(conn, "Barracuda", mbids=[str(uuid4())])
    confirmation(conn, heart, "Crazy On You", T0)
    song(conn, "Battery", recording_mbid(1), release_mbid(1))
    answers(fill, release_mbid(1))

    fill.run()

    assert fill.server.requests == [
        Asked("name_title", ("HEART",), "Barracuda"),
        Asked("name_title", ("HEART",), "Crazy On You"),
        Asked("release", mbid=release_mbid(1)),
        Asked("name_title", ("HEART",), "Magic Man"),
    ]
    assert [seen.progress and seen.progress["tier"] for seen in fill.server.seen] == [
        "payoff",
        "confirmations",
        "work_links",
        "tail",
    ]


def test_the_tier_is_capped_per_run(conn: Conn, fill: Fill) -> None:
    """R3-a: the cap counts the tier's calls in the run (releases, not recordings); the rest
    waits for the next run."""
    fill.use(limits(caps={FillTier.WORK_LINKS: 2}))
    for n in (1, 2, 3):
        song(conn, f"Song {n}", recording_mbid(n), release_mbid(n))
    answers(fill, release_mbid(1), release_mbid(2), release_mbid(3))

    fill.run()

    assert asked_releases(fill) == [release_mbid(1), release_mbid(2)]
    assert fill.last()["detail"] == "work_links, 2 of 2 (cap 2)"
    assert again(fill) == [release_mbid(3)]


# --- the work list -------------------------------------------------------------------------------


def test_the_work_list_is_performances_with_no_known_work_link(conn: Conn, fill: Fill) -> None:
    """R3-b, S5: a performance whose work isn't a MusicBrainz work, and whose recording no live
    ``recording:`` row names a work for and no live ``recording-work:`` row covers, is looked up
    through the release a present file of it names. A row expired is no row (C42)."""
    asked = [release_mbid(n) for n in (1, 2, 3, 4)]
    song(conn, "Nothing known", recording_mbid(1), release_mbid(1))
    song(conn, "Looked up, no work", recording_mbid(2), release_mbid(2))
    cached(conn, f"recording:{recording_mbid(2)}", data={"id": recording_mbid(2)})
    song(conn, "Fetched, expired", recording_mbid(3), release_mbid(3))
    cached(conn, work_key(recording_mbid(3)), expired=True, data=UNLINKED)
    song(conn, "Looked up, expired", recording_mbid(4), release_mbid(4))
    cached(
        conn,
        f"recording:{recording_mbid(4)}",
        expired=True,
        data={"id": recording_mbid(4), "relations": [performs(work_mbid(4), "Expired")]},
    )

    song(conn, "MusicBrainz work", recording_mbid(5), release_mbid(5), musicbrainz=work_mbid(5))
    song(conn, "Looked up", recording_mbid(6), release_mbid(6))
    cached(
        conn,
        f"recording:{recording_mbid(6)}",
        data={"id": recording_mbid(6), "relations": [performs(work_mbid(6), "Looked up")]},
    )
    song(conn, "Fetched", recording_mbid(7), release_mbid(7))
    cached(conn, work_key(recording_mbid(7)), data=linked(work_mbid(7), "Fetched"))
    song(conn, "Fetched, no work", recording_mbid(8), release_mbid(8))
    cached(conn, work_key(recording_mbid(8)), data=UNLINKED)
    song(conn, "Its file missing", recording_mbid(9), release_mbid(9), status="missing")
    song(conn, "No release named", recording_mbid(10))
    file_on(conn, work(conn, "No release tag"), None, recording_mbid(10), None)
    no_recording = work(conn, "No recording")
    file_on(conn, no_recording, performance(conn, no_recording, None), None, release_mbid(11))
    file_on(conn, work(conn, "No performance"), None, recording_mbid(12), release_mbid(12))
    answers(fill, *asked)

    fill.run()

    assert asked_releases(fill) == asked


def test_the_release_is_a_present_files(conn: Conn, fill: Fill) -> None:
    """R3-b: a missing file's release isn't looked up; the present file's is."""
    sung = song(conn, "Battery", recording_mbid(1), release_mbid(2))
    file_on(
        conn,
        sung.work_id,
        sung.performance_id,
        recording_mbid(1),
        release_mbid(1),
        status="missing",
    )
    fill.server.releases[release_mbid(2)] = release(release_mbid(2), [track(recording_mbid(1))])

    fill.run()

    assert asked_releases(fill) == [release_mbid(2)]


def test_a_release_musicbrainz_doesnt_have_is_left_out(conn: Conn, fill: Fill) -> None:
    """R3-b, E2: a release with a live ``release-missing:`` marker isn't asked again, and its
    recording stays unlinked; an expired marker is no marker."""
    song(conn, "Battery", recording_mbid(1), release_mbid(1))
    cached(conn, f"release-missing:{release_mbid(1)}", data={"status": 404})
    song(conn, "Orion", recording_mbid(2), release_mbid(2))
    cached(conn, f"release-missing:{release_mbid(2)}", expired=True, data={"status": 404})
    answers(fill, release_mbid(2))

    fill.run()

    assert asked_releases(fill) == [release_mbid(2)]
    assert cache_keys(conn, "recording-work:") == {work_key(recording_mbid(2))}


# --- the order -----------------------------------------------------------------------------------


def test_the_release_covering_most_goes_first(conn: Conn, fill: Fill) -> None:
    """R3-c: the release covering the most unlinked recordings goes first, ties by its MBID, and
    a later release counts only the recordings still left: here 2 lookups cover the 4
    recordings 5 releases carry."""
    song(conn, "One", recording_mbid(1), release_mbid(1), release_mbid(2))
    song(conn, "Two", recording_mbid(2), release_mbid(2), release_mbid(3))
    song(conn, "Three", recording_mbid(3), release_mbid(2), release_mbid(3))
    song(conn, "Four", recording_mbid(4), release_mbid(5), release_mbid(4))
    fill.server.releases[release_mbid(2)] = release(
        release_mbid(2), [track(recording_mbid(n)) for n in (1, 2, 3)]
    )
    fill.server.releases[release_mbid(4)] = release(release_mbid(4), [track(recording_mbid(4))])

    fill.run()

    assert asked_releases(fill) == [release_mbid(2), release_mbid(4)]
    assert cache_keys(conn, "recording-work:") == {
        work_key(recording_mbid(n)) for n in (1, 2, 3, 4)
    }


# --- the call ------------------------------------------------------------------------------------


def test_a_release_is_asked_for_the_recordings_still_unlinked(conn: Conn, fill: Fill) -> None:
    """R3-d: the call's keys are the ``recording-work:`` keys of the recordings it's planned for,
    so a release one of whose tracks is already linked is still asked for the others, and its
    lookup writes every track's row again."""
    song(conn, "Battery", recording_mbid(1), release_mbid(1))
    cached(conn, work_key(recording_mbid(1)), data=linked(work_mbid(1), "Battery"))
    song(conn, "Orion", recording_mbid(2), release_mbid(1))
    fill.server.releases[release_mbid(1)] = release(
        release_mbid(1),
        [track(recording_mbid(1), performs(work_mbid(1), "Battery")), track(recording_mbid(2))],
    )

    fill.run()

    assert asked_releases(fill) == [release_mbid(1)]
    assert cache_entry(conn, work_key(recording_mbid(2))) == UNLINKED


def test_each_batch_plans_from_the_rows_written(conn: Conn, fill: Fill) -> None:
    """R3-d, R8: a lookup's rows are committed with its call, and the next batch plans again from
    them, so a recording an earlier release carried isn't looked up again through its own."""
    fill.use(limits(batch_calls=2))
    song(conn, "One", recording_mbid(1), release_mbid(1))
    song(conn, "Two", recording_mbid(2), release_mbid(2))
    song(conn, "Three", recording_mbid(3), release_mbid(3))  # release 1 carries it too
    fill.server.releases[release_mbid(1)] = release(
        release_mbid(1), [track(recording_mbid(1)), track(recording_mbid(3))]
    )
    answers(fill, release_mbid(2), release_mbid(3))

    batches = fill.run()

    assert asked_releases(fill) == [release_mbid(1), release_mbid(2)]
    assert batches == 2
    assert fill.server.seen[1].cached == 3  # release 1 and its two tracks' rows
    assert fill.last()["ended"] == "done"


def test_a_cached_release_costs_no_request(conn: Conn, fill: Fill) -> None:
    """R3-e: a release already cached (as enrichment caches one) answers with no request, and
    its tracks' rows are written all the same; the call counts against the cap."""
    one = release_mbid(1)
    song(conn, "Battery", recording_mbid(1), one)
    cached(
        conn,
        f"release:{one}",
        data=release(one, [track(recording_mbid(1), performs(work_mbid(1), "Battery"))]),
    )

    fill.run()

    assert fill.server.requests == []
    assert cache_entry(conn, work_key(recording_mbid(1))) == linked(work_mbid(1), "Battery")
    last = fill.last()
    assert (nonzero(last["calls"]), last["requests"]) == ({"work_links": 1}, 0)


# --- when MusicBrainz has no answer --------------------------------------------------------------


def test_a_404_is_remembered_and_another_release_asked_next(conn: Conn, fill: Fill) -> None:
    """R3-f, R3-j: a release MusicBrainz no longer has is an answer: its ``release-missing:``
    marker, no ``recording-work:`` row, no pause, no failure. Its recording waits for another
    release a file of it names, asked the next time the work list is planned: here the next
    run, as the run's one plan held nothing else."""
    gone, kept = release_mbid(1), release_mbid(2)
    song(conn, "Battery", recording_mbid(1), gone, kept)
    fill.server.releases[kept] = release(
        kept, [track(recording_mbid(1), performs(work_mbid(1), "Battery"))]
    )

    fill.run()

    first = fill.last()
    assert asked_releases(fill) == [gone]
    assert (first["ended"], nonzero(first["failed"]), first["requests"]) == ("done", {}, 1)
    assert fill.server.pauses == []
    assert cache_keys(conn, "release") == {f"release-missing:{gone}"}
    assert cache_keys(conn, "recording-work:") == set()
    assert stored_as(conn, f"release-missing:{gone}") == ("release-missing", gone)

    assert again(fill) == [kept]
    assert cache_entry(conn, work_key(recording_mbid(1))) == linked(work_mbid(1), "Battery")
    assert again(fill) == []


def test_a_release_without_the_recording_is_not_asked_for_it_again(conn: Conn, fill: Fill) -> None:
    """R3-i, R3-j: a release MusicBrainz answers without a recording a file named on it (one
    since merged) writes its tracks' rows and none for that recording. Its live ``release:`` row
    keeps it out of the recording's work list, so the recording is asked through another
    release a file of it names, or stays unlinked; it never costs another call."""
    first, other, only = release_mbid(1), release_mbid(2), release_mbid(3)
    song(conn, "Battery", recording_mbid(1), first, other)
    song(conn, "Orion", recording_mbid(2), first)
    song(conn, "Disposable Heroes", recording_mbid(3), only)
    fill.server.releases[first] = release(first, [track(recording_mbid(2))])
    fill.server.releases[other] = release(
        other, [track(recording_mbid(1), performs(work_mbid(1), "Battery"))]
    )
    fill.server.releases[only] = release(only, [track(recording_mbid(9))])

    fill.run()

    assert asked_releases(fill) == [first, only]
    assert cache_keys(conn, "recording-work:") == {
        work_key(recording_mbid(2)),
        work_key(recording_mbid(9)),
    }
    assert again(fill) == [other]
    assert cache_entry(conn, work_key(recording_mbid(1))) == linked(work_mbid(1), "Battery")
    assert again(fill) == []
    assert nonzero(fill.last()["calls"]) == {}


def test_after_a_miss_the_next_batch_asks_another_release(conn: Conn, fill: Fill) -> None:
    """R3-j, D16: a batch's plan is fixed and the next batch plans again, so after a 404 or a
    release without the recording (R3-i) the recording's other release is asked in the next
    batch of the same run. A refused release's keys are the run's missed keys (AUD-R093), so its
    recording isn't asked through its other release in that run: it waits for the next run,
    which asks the release the order picks (here the refused one, first by its MBID)."""
    fill.use(limits(batch_calls=1))
    refused, second = release_mbid(1), release_mbid(2)
    gone, kept = release_mbid(3), release_mbid(4)
    without, other = release_mbid(5), release_mbid(6)
    last = release_mbid(7)
    song(conn, "Battery", recording_mbid(1), refused, second)
    song(conn, "Orion", recording_mbid(2), gone, kept)
    song(conn, "Damage Inc", recording_mbid(3), without, other)
    song(conn, "Disposable Heroes", recording_mbid(4), last)  # asked last: the run goes on to it
    carried = {refused: 1, second: 1, kept: 2, without: 9, other: 3, last: 4}
    for mbid, n in carried.items():
        fill.server.releases[mbid] = release(mbid, [track(recording_mbid(n))])
    fill.server.fail = {refused: "503"}

    batches = fill.run()

    assert asked_releases(fill) == [refused, gone, kept, without, other, last]
    assert batches == 6  # one call a batch: each other release comes in the batch after the miss
    assert fill.server.pauses == [(1, 10.0)]
    assert cache_keys(conn, "recording-work:") == {
        work_key(recording_mbid(n)) for n in (2, 3, 4, 9)
    }
    end = fill.last()
    assert (end["ended"], end["failed_calls"]) == ("done", 1)

    fill.server.fail = {}
    assert again(fill) == [refused]
    assert work_key(recording_mbid(1)) in cache_keys(conn, "recording-work:")
    assert again(fill) == []


def test_a_refused_release_is_one_request_and_waits_for_the_next_run(
    conn: Conn, fill: Fill
) -> None:
    """AUD-R093 through R3-d: a refused lookup is one request, the pause follows it, nothing is
    written for it, and the failure is counted on the row; its recordings wait for the next run,
    which asks again."""
    refused, answered = release_mbid(1), release_mbid(2)
    song(conn, "Battery", recording_mbid(1), refused)
    song(conn, "Orion", recording_mbid(2), answered)
    answers(fill, refused, answered)
    fill.server.fail = {refused: "503"}

    fill.run()

    assert asked_releases(fill) == [refused, answered]
    assert fill.server.pauses == [(1, 10.0)]
    assert cache_keys(conn, "release") == {f"release:{answered}"}
    assert cache_keys(conn, "recording-work:") == {work_key(recording_mbid(2))}
    last = fill.last()
    assert last["detail"] == "work_links, 2 of 2 (cap 3,000), 1 failed"
    assert (last["warning"], last["failed_calls"], last["requests"]) == (
        "mb_fill_calls_failed",
        1,
        2,
    )

    fill.server.fail = {}
    assert again(fill) == [refused]
    assert cache_keys(conn, "recording-work:") == {
        work_key(recording_mbid(1)),
        work_key(recording_mbid(2)),
    }


def test_a_400_is_skipped_for_the_run(conn: Conn, fill: Fill) -> None:
    """M5 through R3-d: a 4xx other than 404 and 429 is skipped for the run, unpaused and
    unremembered; the next run asks again."""
    song(conn, "Battery", recording_mbid(1), release_mbid(1))
    answers(fill, release_mbid(1))
    fill.server.fail = {release_mbid(1): "400"}

    fill.run()

    last = fill.last()
    assert nonzero(last["skipped"]) == {"work_links": 1}
    assert fill.server.pauses == []
    assert cache_keys(conn, "release") == set()
    assert cache_keys(conn, "recording-work:") == set()
    fill.server.fail = {}
    assert again(fill) == [release_mbid(1)]


def test_five_refusals_in_a_row_end_the_run(conn: Conn, fill: Fill) -> None:
    """AUD-R091's cut-off, through AUD-R093's pause: five refused lookups in a row end the run,
    its last row saying so (D8)."""
    for n in range(1, 7):
        song(conn, f"Song {n}", recording_mbid(n), release_mbid(n))
    fill.server.down = "503"

    fill.run()

    assert asked_releases(fill) == [release_mbid(n) for n in range(1, 6)]
    assert fill.server.pauses == [(n, 10.0) for n in range(1, 5)]
    last = fill.last()
    assert last["detail"] == "work_links, stopped (MusicBrainz not answering)"
    assert (last["ended"], last["cut_off"]) == ("cut_off", True)


# --- the merge list ------------------------------------------------------------------------------


def test_the_merge_list_plans_what_the_links_join(conn: Conn, fill: Fill) -> None:
    """S5, D1: the merge list plans the works a release's links join on its next load, and
    nothing merges until Lance's Apply (AUD-R080)."""
    set_marker(conn, WORK_ARTISTS_CREDITED_SETTING)
    one = release_mbid(1)
    labelle = song(conn, "Lady Marmalade", recording_mbid(1), one)
    aguilera = song(conn, "Lady Marmalade", recording_mbid(2), one)
    fill.server.releases[one] = release(
        one,
        [
            track(recording_mbid(1), performs(work_mbid(1), "Lady Marmalade")),
            track(recording_mbid(2), performs(work_mbid(1), "Lady Marmalade")),
        ],
    )

    def groups() -> list[tuple[frozenset[str], frozenset[MergeJoin]]]:
        repos = RepositoryFactory(conn)
        found = plan(repos.merge_list, repos.user_settings).groups
        return [(frozenset({g.target_id, *g.source_ids}), g.joins) for g in found]

    def merges() -> int:
        """Logged merges. The suite's cleanup doesn't empty the merge log (no FK to works), so
        an earlier test's merges may still be there: the run must add none."""
        row = conn.execute("SELECT count(*) AS n FROM work_merges").fetchone()
        assert row is not None
        return int(row["n"])

    assert groups() == []
    logged = merges()

    fill.run()

    assert groups() == [
        (frozenset({labelle.work_id, aguilera.work_id}), frozenset({MergeJoin.WORK_LINK}))
    ]
    assert merges() == logged

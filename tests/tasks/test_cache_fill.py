"""The MusicBrainz cache fill's runs (phase 4.1 PR 2; SPEC S3, S4, S7, E1, E2, E4, E5; D3, D4,
D6; M1-M11 as the coordinator read them; AUD-R014, AUD-R029, AUD-R042, AUD-R091), on
PostgreSQL. ``test_cache_fill_tiers_pg.py`` has what each tier searches.

- **A run** is queued by name: ``mb_cache_fill_task()`` (D6), which the cutover runbook starts.
  While one is in progress (a batch running, or the next one queued) a second is refused and
  logged.
- **A batch** is ``mb_cache_fill_batch_task``, at Huey priority -10, below every other task. It
  works through the tiers in order: payoff, C68, C18, confirmations, tail, then the alias step.
  It skips a query with a live cache entry (C42: the work list is "no cache entry") and calls
  each key once.
  - It makes at most 100 calls, and starts none once 110 s have passed on its clock.
  - Each call's cache row is committed as it lands (R8).
  - It hands the run's next batch on through ``enqueue_or_log`` while capped work remains.
- **A tier** stops at its cap for the run. What it leaves waits for the next run.
- **Failures:**
  - Five transient failures in a row end the run (E1): a timeout, a network error, or a 429/5xx
    the client's own retries gave up on. The count runs across the run's batches, and an answer
    resets it.
  - A permanent error (a 4xx other than 429) is logged and its item skipped for the run (M5),
    neither counted toward the cut-off nor resetting it.
  - A failed or skipped item is not asked again in the same run; the next run asks it again.
- **The row:** while a batch runs, its row names the tier, the calls made of the batch's and
  the tier's cap (D4). Each batch's last row carries the run's calls, failures and skips per
  tier so far, and whether the run was cut off.
- **Re-checks:** as the run leaves a tier, the names that tier filled are re-checked by one
  ``rematch_names_task`` (S7), also at a cut-off; a tier that filled none queues none.
- **The alias step** (D3, R10) asks every catalog MBID with no ``artist-aliases:`` entry, 100 a
  request, and writes each alias as a ``MUSICBRAINZ_ALIAS`` name of the artist holding the MBID.

The real client runs on a mock transport (``cache_fill_rig``). Counts of requests are counts of
calls, except where a 503 is retried: then each call is three requests.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from structlog.testing import capture_logs

from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.domain.enums import FillTier, TaskStatus, TaskType
from backend.domain.system import TaskProgress
from tests.integration.cache_fill_rig import (
    Asked,
    Conn,
    Fill,
    Name,
    cache_entry,
    cache_keys,
    cached,
    catalog_artist,
    confirmation,
    key,
    library_file,
    limits,
    line,
    link,
    log_name,
    nonzero,
)

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 22, 9, tzinfo=UTC)
SANTANA = "a3b2c1d0-0000-4000-8000-000000000030"
BOSTON = "a3b2c1d0-0000-4000-8000-000000000031"
NATO = ("ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT", "GOLF", "HOTEL", "INDIA", "JULIET")


def mbid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


@pytest.fixture
def fill(conn: Conn, migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Fill]:
    rig = Fill(conn, migrated_db, monkeypatch)
    yield rig
    rig.close()


def payoff(
    conn: Conn, original: str, title: str, plays: int, *, status: str = "needs_review"
) -> Name:
    """A payoff pair: *original* played *title* *plays* times, and one act's file holds it."""
    name = log_name(conn, original, status)
    line(conn, name, title, plays=plays)
    library_file(conn, title, mbids=[str(uuid4())])
    return name


def payoffs(conn: Conn, count: int) -> list[Name]:
    """*count* payoff pairs, ALPHA's "Song 1" the most played."""
    return [payoff(conn, NATO[n], f"Song {n + 1}", 100 - n) for n in range(count)]


def songs(count: int) -> list[str]:
    return [f"Song {n + 1}" for n in range(count)]


def names_of(conn: Conn, artist_id: str) -> set[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT name, normalized_name, source FROM artist_names WHERE artist_id = %s",
        (artist_id,),
    ).fetchall()
    return {(r["name"], r["normalized_name"], r["source"]) for r in rows}


def alias(name: str, sort_name: str) -> dict[str, Any]:
    return {"name": name, "sort-name": sort_name, "type": "Artist name", "locale": "en"}


# --- S3: the tiers ------------------------------------------------------------------------------


def test_tier_order_and_skip(conn: Conn, fill: Fill) -> None:
    """S3: payoff, C68, C18, confirmations, tail, then the alias step. A query with a live cache
    entry is skipped; an expired one is asked again (C42)."""
    payoff(conn, "QUEEN & DAVID BOWIE", "Under Pressure", 50)
    payoff(conn, "TOM PETTY", "Free Fallin'", 40)
    cached(conn, key("TOM PETTY", "Free Fallin'"))
    payoff(conn, "HEART", "Barracuda", 30, status="auto_rejected")
    cached(conn, key("HEART", "Barracuda"), expired=True)
    library_file(conn, "The Boxer", artist="Simon & Garfunkel")  # C68
    santana = catalog_artist(conn, "Santana", SANTANA)
    featuring = log_name(conn, "SANTANA FT. ROB THOMAS", "manual_matched")
    link(conn, featuring, santana, origin="manual")
    line(conn, featuring, "Smooth", plays=20)  # C18
    boston = log_name(conn, "BOSTON", "auto_matched")
    link(conn, boston, catalog_artist(conn, "Boston", BOSTON))
    confirmation(conn, boston, "More Than a Feeling", T0)  # confirmations
    foghat = log_name(conn, "FOGHAT")
    line(conn, foghat, "Slow Ride", plays=10)  # tail: no file holds it

    fill.run()

    assert fill.server.requests == [
        Asked("name_title", ("QUEEN", "DAVID", "BOWIE"), "Under Pressure"),
        Asked("name_title", ("HEART",), "Barracuda"),
        Asked("name_title", ("Simon", "Garfunkel"), "The Boxer"),
        Asked("recording", title="smooth", mbid=SANTANA),
        Asked("name_title", ("BOSTON",), "More Than a Feeling"),
        Asked("name_title", ("FOGHAT",), "Slow Ride"),
        Asked("aliases", mbids=frozenset({SANTANA, BOSTON})),
    ]


def test_exclusions(conn: Conn, fill: Fill) -> None:
    """S3 and the names searched: names in review or auto rejected (the set re-checks rewind),
    less the not-an-artist list (0045's seeded names and a curator's). A pending name waits for
    the local stage; a manually rejected one is never searched (AUD-R029), in any tier; a name
    with an active link is not searched (a manual one only through C18)."""
    payoff(conn, "BON JOVI", "Livin' on a Prayer", 9)
    payoff(conn, "JOURNEY", "Separate Ways", 8, status="auto_rejected")
    styx = payoff(conn, "STYX", "Renegade", 7, status="manual_rejected")
    line(conn, styx, "Too Much Time on My Hands", plays=7)  # would be tail
    confirmation(conn, styx, "Babe", T0)  # would be a confirmation
    various = payoff(conn, "VARIOUS ARTISTS", "Greatest Hits", 6)
    confirmation(conn, various, "More Hits", T0)
    kansas = payoff(conn, "KANSAS", "Carry On Wayward Son", 5, status="pending")
    line(conn, kansas, "Point of Know Return", plays=5)
    rush = payoff(conn, "RUSH", "Tom Sawyer", 4, status="auto_matched")
    link(conn, rush, catalog_artist(conn, "Rush"))
    yes = payoff(conn, "YES", "Roundabout", 3, status="manual_matched")
    link(conn, yes, catalog_artist(conn, "Yes"), origin="manual")
    loverboy = payoff(conn, "LOVERBOY", "Working for the Weekend", 2)
    confirmation(conn, loverboy, "Turn Me Loose", T0)
    conn.execute(
        """INSERT INTO log_name_placeholders (normalized_name, added_by)
           VALUES ('loverboy', 'curator')"""
    )
    try:
        fill.run()
    finally:
        conn.execute("DELETE FROM log_name_placeholders WHERE normalized_name = 'loverboy'")
        conn.commit()

    assert fill.server.requests == [
        Asked("name_title", ("BON", "JOVI"), "Livin' on a Prayer"),
        Asked("name_title", ("JOURNEY",), "Separate Ways"),
    ]


def test_aliases_written(conn: Conn, fill: Fill) -> None:
    """D3, R10: every catalog artist holding a real MBID with no ``artist-aliases:`` entry is
    asked, 100 MBIDs a request. Each alias becomes a ``musicbrainz_alias`` name of the artist
    holding the MBID, normalized as artist names are. An MBID the index doesn't know is cached as
    unknown and names nothing. A special-purpose MBID (C7) and an artist with no MBID are never
    asked. The step fills no log name, so it queues no re-check; a second run asks nothing."""
    mellencamp_mbid = mbid(500)
    gone_mbid = mbid(501)
    mellencamp = catalog_artist(conn, "John Mellencamp", mellencamp_mbid)
    prince = catalog_artist(conn, "Prince", mbid(502))
    gone = catalog_artist(conn, "Merged Away", gone_mbid)
    catalog_artist(conn, "[unknown]", "125ec42a-7229-4250-afc5-e057484327fe")
    catalog_artist(conn, "Local Band")
    catalog_artist(conn, "Asked Before", mbid(503))
    cached(conn, f"artist-aliases:{mbid(503)}", data={"id": mbid(503), "aliases": []})
    others = [mbid(1000 + n) for n in range(98)]
    for n, other in enumerate(others):
        catalog_artist(conn, f"Act {n}", other)
    fill.server.aliases[mellencamp_mbid] = [
        alias("John Cougar", "Cougar, John"),
        alias("John Cougar Mellencamp", "Mellencamp, John Cougar"),
        alias("Johnny Cougar", "Cougar, Johnny"),
    ]
    fill.server.unknown.add(gone_mbid)

    fill.run()

    requests = fill.server.asked("aliases")
    assert sorted(len(r.mbids) for r in requests) == [1, 100]
    assert frozenset().union(*(r.mbids for r in requests)) == {
        mellencamp_mbid,
        gone_mbid,
        mbid(502),
        *others,
    }
    assert names_of(conn, mellencamp) == {
        ("John Cougar", "john cougar", "musicbrainz_alias"),
        ("John Cougar Mellencamp", "john cougar mellencamp", "musicbrainz_alias"),
        ("Johnny Cougar", "johnny cougar", "musicbrainz_alias"),
    }
    assert names_of(conn, prince) == set()
    assert names_of(conn, gone) == set()
    assert cache_entry(conn, f"artist-aliases:{gone_mbid}") == {"not_in_index": True}
    assert fill.rechecks() == []

    fill.server.requests.clear()
    fill.run()
    assert fill.server.requests == []


# --- S4: batches, caps, the clock and the cut-off ------------------------------------------------


def test_batches_caps_and_cutoff(conn: Conn, fill: Fill) -> None:
    """S4: a run is a chain of batches at Huey priority -10, each making at most its calls; each
    tier stops at its cap for the run, and what it leaves waits for the next run, whose caps
    start again. The last row carries the run's calls per tier."""
    fill.use(limits(batch_calls=2, caps={FillTier.PAYOFF: 3, FillTier.TAIL: 1}))
    for n, title in enumerate(("One", "Two", "Three", "Four")):
        payoff(conn, NATO[n], title, 40 - n)
    for n, title in enumerate(("Five", "Six")):
        line(conn, log_name(conn, NATO[4 + n]), title, plays=9 - n)

    assert fill.run() == 2
    assert fill.server.titles() == ["One", "Two", "Three", "Five"]
    assert [t.priority for t in fill.batches_queued()] == [-10, -10]
    rows = fill.rows()
    assert [r["status"] for r in rows] == ["completed", "completed"]
    assert nonzero(rows[-1]["calls"]) == {"payoff": 3, "tail": 1}
    assert rows[-1]["cut_off"] is False

    fill.server.requests.clear()
    assert fill.run() == 1
    assert fill.server.titles() == ["Four", "Six"]


def test_no_call_starts_after_110_seconds(conn: Conn, fill: Fill) -> None:
    """S4 (R9): a batch starts no call once 110 s have passed on its clock, so a batch of slow
    answers stays near 2 minutes; the next batch takes the rest."""
    payoffs(conn, 5)
    fill.server.seconds_per_call = 50.0

    fill.start()
    fill.batch()
    assert fill.server.titles() == songs(3)  # started at 0, 50 and 100 s
    assert len(fill.batches_queued()) == 2
    fill.batch()
    assert fill.server.titles() == songs(5)


def test_progress_names_the_tier_the_batch_and_the_cap(conn: Conn, fill: Fill) -> None:
    """D4, M10: while a batch runs, its row names the tier, the calls made of the batch's, and
    the tier's cap for the run ("payoff, 37 of 100 (cap 2,600)"); the uncapped alias step names
    no cap."""
    payoffs(conn, 2)
    catalog_artist(conn, "Prince", mbid(502))

    fill.run()

    first, second, third = (seen.progress for seen in fill.server.seen)
    assert first is not None and first["detail"] == "payoff, 0 of 3 (cap 2,600)"
    assert second is not None
    assert {k: second[k] for k in ("tier", "processed", "total", "cap", "detail")} == {
        "tier": "payoff",
        "processed": 1,
        "total": 3,
        "cap": 2600,
        "detail": "payoff, 1 of 3 (cap 2,600)",
    }
    assert third is not None and third["detail"] == "aliases, 2 of 3"


def test_each_call_is_committed_as_it_lands(conn: Conn, fill: Fill) -> None:
    """S4 (R8): each call's cache row is committed before the next call starts."""
    payoffs(conn, 3)

    fill.run()

    assert [seen.cached for seen in fill.server.seen] == [0, 1, 2]


# --- S7: the re-checks ----------------------------------------------------------------------------


def test_recheck_handoff(conn: Conn, fill: Fill) -> None:
    """S7 (R6): as the run leaves a tier, one ``rematch_names_task`` re-checks the names that
    tier filled (their normalized names). C68 (files) and the alias step fill no names and
    queue none."""
    bon_jovi = payoff(conn, "BON JOVI", "Livin' on a Prayer", 9)
    journey = payoff(conn, "JOURNEY", "Separate Ways", 8, status="auto_rejected")
    library_file(conn, "The Boxer", artist="Simon & Garfunkel")
    featuring = log_name(conn, "SANTANA FT. ROB THOMAS", "manual_matched")
    link(conn, featuring, catalog_artist(conn, "Santana", SANTANA), origin="manual")
    line(conn, featuring, "Smooth", plays=5)
    foghat = log_name(conn, "FOGHAT")
    line(conn, foghat, "Slow Ride", plays=4)

    fill.run()

    assert fill.rechecks() == [
        sorted([bon_jovi.normalized, journey.normalized]),
        [featuring.normalized],
        [foghat.normalized],
    ]


def test_a_cut_off_rechecks_what_its_tier_filled(conn: Conn, fill: Fill) -> None:
    """S7, M4: a cut-off ends the run in the middle of a tier; the names it filled so far are
    re-checked. A tier that filled none queues none."""
    names = payoffs(conn, 7)
    fill.server.script = [None, *["timeout"] * 5]

    fill.run()

    assert fill.rechecks() == [[names[0].normalized]]

    fill.server.requests.clear()
    fill.server.script = ["timeout"] * 5
    fill.run()
    assert fill.rechecks() == [[names[0].normalized]]  # nothing more


# --- E1, M5: failures -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outage", "requests_per_call"), [("timeout", 1), ("connect", 1), ("503", 3)]
)
def test_resume_after_outage(conn: Conn, fill: Fill, outage: str, requests_per_call: int) -> None:
    """E1: 5 transient failures in a row (a timeout, a network error, or a 503 the client's own
    retries gave up on) end the run, its last row saying so. Nothing is cached for them, so the
    next run asks them again, most plays first."""
    payoffs(conn, 7)
    fill.server.down = outage

    assert fill.run() == 1
    assert len(fill.server.requests) == 5 * requests_per_call
    assert cache_keys(conn, "recording-name-title:") == set()
    last = fill.last()
    assert last["status"] == "completed"
    assert last["cut_off"] is True
    assert nonzero(last["failed"]) == {"payoff": 5}

    fill.server.down = None
    fill.server.requests.clear()
    fill.run()
    assert fill.server.titles() == songs(7)


def test_the_cut_off_counts_failures_across_batches(conn: Conn, fill: Fill) -> None:
    """E1: the count of failures in a row runs on across the run's batches, so an outage of slow
    timeouts (4 to a batch) still ends the run at its fifth."""
    payoffs(conn, 8)
    fill.server.down = "timeout"
    fill.server.seconds_per_call = 30.0

    assert fill.run() == 2
    assert fill.server.titles() == songs(5)  # 4 in the first batch, at 0, 30, 60 and 90 s
    last = fill.last()
    assert last["cut_off"] is True
    # D8: the run's last row says it stopped, so the bar's green Done never hides an outage.
    assert last["detail"] == "payoff, stopped (MusicBrainz not answering)"


def test_an_answer_resets_the_failure_count(conn: Conn, fill: Fill) -> None:
    """E1: any answer resets the count. A name whose call failed waits for the next run: it is
    not asked again in this one."""
    payoffs(conn, 10)
    fill.server.script = [*["timeout"] * 4, None, *["timeout"] * 4, None]

    fill.run()

    assert fill.server.titles() == songs(10)
    last = fill.last()
    assert last["cut_off"] is False
    assert nonzero(last["failed"]) == {"payoff": 8}

    fill.server.requests.clear()
    fill.run()
    assert fill.server.titles() == [t for t in songs(10) if t not in ("Song 5", "Song 10")]


def test_a_permanent_error_skips_the_item_for_the_run(conn: Conn, fill: Fill) -> None:
    """M5: a 4xx other than 429 is logged, and its item skipped for the rest of the run: not
    cached, and counted in the run's progress and its last row, so a pattern repeating run after
    run shows. A run whose remaining work all failed or was skipped ends; the next run asks those
    items again."""
    fill.use(limits(batch_calls=2))
    payoff(conn, "ALPHA", "One", 9)
    payoff(conn, "BRAVO", "Two", 8)
    payoff(conn, "CHARLIE", "Three", 7)
    fill.server.fail = {"Two": "400", "Three": "timeout"}

    with capture_logs() as logs:
        assert fill.run() == 2

    assert fill.server.titles() == ["One", "Two", "Three"]
    assert cache_keys(conn, "recording-name-title:") == {key("ALPHA", "One")}
    assert any(e["log_level"] == "warning" and "two" in str(e).lower() for e in logs)
    during = fill.server.seen[2].progress  # the second batch's first call
    assert during is not None and nonzero(during["skipped"]) == {"payoff": 1}
    last = fill.last()
    assert nonzero(last["calls"]) == {"payoff": 3}
    assert nonzero(last["skipped"]) == {"payoff": 1}
    assert nonzero(last["failed"]) == {"payoff": 1}

    fill.server.requests.clear()
    fill.run()
    assert fill.server.titles() == ["Two", "Three"]


def test_a_permanent_error_neither_counts_toward_the_cut_off_nor_resets_it(
    conn: Conn, fill: Fill
) -> None:
    """M5: four timeouts, a 400, then a fifth timeout: cut off."""
    payoffs(conn, 8)
    fill.server.script = [*["timeout"] * 4, "400", "timeout"]

    fill.run()

    assert fill.server.titles() == songs(6)
    assert fill.last()["cut_off"] is True


# --- E2, E4, E5 -----------------------------------------------------------------------------------


def test_empty_result_cached(conn: Conn, fill: Fill) -> None:
    """E2: a search that finds nothing is cached as an empty result and never asked again."""
    payoff(conn, "ALPHA", "Nothing Here", 3)

    fill.run()

    assert cache_entry(conn, key("ALPHA", "Nothing Here")) == {
        "count": 0,
        "title_only": False,
        "recordings": [],
    }
    fill.server.requests.clear()
    fill.run()
    assert fill.server.requests == []


def test_restart(conn: Conn, fill: Fill) -> None:
    """E4: the worker dies mid-batch. The calls already made stay cached (each was committed as it
    landed); the next run starts from the work list and asks only the rest."""
    payoffs(conn, 4)
    fill.server.script = [None, None, "crash"]

    with pytest.raises(RuntimeError, match="the worker died"):
        fill.run()

    assert cache_keys(conn, "recording-name-title:") == {
        key("ALPHA", "Song 1"),
        key("BRAVO", "Song 2"),
    }
    assert fill.last()["status"] == "failed"

    fill.server.requests.clear()
    fill.run()
    assert fill.server.titles() == ["Song 3", "Song 4"]


def test_initials_only(conn: Conn, fill: Fill) -> None:
    """E5: a name of only initials or stop words is searched by its title alone, and its entry
    says ``title_only``."""
    payoff(conn, "R.E.M.", "Losing My Religion", 5)
    payoff(conn, "THE THE", "This Is the Day", 4)

    fill.run()

    assert fill.server.requests == [
        Asked("name_title", (), "Losing My Religion"),
        Asked("name_title", (), "This Is the Day"),
    ]
    entry = cache_entry(conn, key("", "Losing My Religion"))
    assert entry is not None and entry["title_only"] is True


# --- D6: starting a run ---------------------------------------------------------------------------


def test_a_second_run_is_refused_while_one_runs(conn: Conn, fill: Fill) -> None:
    """D6: a run is queued by name. While one is in progress, its next batch queued or a batch
    running, a second is refused and logged: nothing asked, nothing queued."""
    fill.use(limits(batch_calls=1))
    payoffs(conn, 2)

    fill.start()
    fill.batch()  # the first batch ran and queued the next
    assert len(fill.batches_waiting()) == 1
    with capture_logs() as logs:
        fill.start()
    assert len(fill.batches_waiting()) == 1
    assert len(fill.server.requests) == 1
    assert any(e["event"] == "mb_cache_fill_refused" for e in logs)
    while fill.batch():
        pass
    assert fill.batches_waiting() == []

    PgTaskProgressRepository(conn).upsert(
        TaskProgress(
            task_id="a-batch-in-progress",
            task_type=TaskType.MB_CACHE_FILL,
            status=TaskStatus.RUNNING,
            progress_data={},
            started_at=T0,
            updated_at=T0,
        )
    )
    conn.commit()
    with capture_logs() as logs:
        fill.start()
    assert fill.batches_waiting() == []
    assert any(e["event"] == "mb_cache_fill_refused" for e in logs)

    conn.execute(
        "UPDATE progress_tracking SET status = 'failed' WHERE task_id = 'a-batch-in-progress'"
    )
    conn.commit()
    fill.start()
    assert len(fill.batches_waiting()) == 1


def test_nothing_to_fill_ends_at_once(conn: Conn, fill: Fill) -> None:
    """DoR (empty): with nothing to fill, the first batch asks nothing and ends the run."""
    assert fill.run() == 1
    assert fill.server.requests == []
    assert fill.rechecks() == []
    assert fill.last()["status"] == "completed"

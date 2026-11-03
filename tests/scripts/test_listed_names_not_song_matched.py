"""No run song-matches a line under a listed name (artist model phase 1c, SPEC S6, E3; C7;
Lance, 2026-11-03).

A local act or library files named like a station placeholder (SPOT BREAK) could match its
spot lines to files, and a matched file can reach the stream. So nothing under a name on the
not-an-artist list is song-matched by any run, and its lines keep their state:

- the matching tasks on the name's playlist (artist, then identity): the artist task's
  DEFERRED_RETRY reset passes a listed name by, so SPOT BREAK and its deferred lines stay
  deferred; and the song stage decides no line of a listed name, whatever the name's status, so
  a pending SPOT BREAK line is not matched by name to a file of that name and title;
- a re-check (Re-run Matching), of either scope: its rewinds pass listed names and their lines
  by, the MusicBrainz-outage sweep (every ``MB_UNAVAILABLE`` line) too, and the runs after it
  match none of them;
- the gate's identity dry run, which does as the task does: it decides no line of a listed name
  (none is in its ``identity-stage`` section).

The names keep their status too (S3: the list hides them). METALLICA, not listed, is matched as
usual beside them. Seeds go through the scan, the ingestion task and the Not an artist route
(the bench database starts empty, the list's seed too); the states the dev database holds
(SPOT BREAK deferred, VARIOUS matched with no link) are set by SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from tests.integration import stream_seed as seed
from tests.scripts.gate_world import (
    Conn,
    file_named,
    ingest,
    line_id,
    load_tool,
    name_id,
    read_json,
    run_artist_matching,
    run_identity_matching,
    scan,
    tagged,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("queue_in_memory")]

DAY = "1995-03-14"
DEFERRED = ("needs_review", "DEFERRED_RETRY")
OUTAGE = ("needs_review", "MB_UNAVAILABLE")


@dataclass(frozen=True)
class World:
    """At an AC station, each line with a library file of its name and title:

    - SPOT BREAK (listed, deferred): JINGLE, deferred with it; STATION ID, pending (a line
      the song stage's by-name step would match to spot-id.mp3 today);
    - VARIOUS (listed, matched with no link): MEDLEY, pending (today the song stage puts it in
      review, as a matched name with no link has no match record); OUTAGE SONG, in review
      after a MusicBrainz outage (``MB_UNAVAILABLE``, which every re-check sweeps);
    - METALLICA / ONE, not listed: matched as usual.
    """

    playlist: UUID
    spot: UUID
    various: UUID
    spot_deferred: UUID
    spot_pending: UUID
    various_line: UUID
    various_outage: UUID
    metallica_line: UUID


def _world(conn: Conn, client: TestClient, tmp_path: Path) -> World:
    root = tmp_path / "library"
    tagged(root / "spot.mp3", "Spot Break", "Jingle")
    tagged(root / "spot-id.mp3", "Spot Break", "Station Id")
    tagged(root / "various.mp3", "Various", "Medley")
    tagged(root / "outage.mp3", "Various", "Outage Song")
    tagged(root / "one.mp3", "Metallica", "One")
    scan(conn, root)
    playlist = ingest(
        conn,
        seed.station(conn, "AC"),
        [
            ("SPOT BREAK", "JINGLE", f"{DAY} 08:00:00"),
            ("SPOT BREAK", "STATION ID", f"{DAY} 08:02:00"),
            ("VARIOUS", "MEDLEY", f"{DAY} 08:04:00"),
            ("VARIOUS", "OUTAGE SONG", f"{DAY} 08:06:00"),
            ("METALLICA", "ONE", f"{DAY} 08:08:00"),
        ],
        "listed",
    )
    world = World(
        playlist=playlist,
        spot=name_id(conn, "SPOT BREAK"),
        various=name_id(conn, "VARIOUS"),
        spot_deferred=line_id(conn, "SPOT BREAK", "JINGLE"),
        spot_pending=line_id(conn, "SPOT BREAK", "STATION ID"),
        various_line=line_id(conn, "VARIOUS", "MEDLEY"),
        various_outage=line_id(conn, "VARIOUS", "OUTAGE SONG"),
        metallica_line=line_id(conn, "METALLICA", "ONE"),
    )
    for name in (world.spot, world.various):
        assert client.post(f"/api/v1/matching/artists/{name}/not-an-artist").status_code == 200
    # As on the dev database: SPOT BREAK deferred with its line (the DEFERRED_RETRY cascade),
    # VARIOUS matched with no link (0045 deleted it); and a line an outage left in review.
    for table, row, (status, reason) in (
        ("broadcast_artists", world.spot, DEFERRED),
        ("track_identities", world.spot_deferred, DEFERRED),
        ("broadcast_artists", world.various, ("auto_matched", None)),
        ("track_identities", world.various_outage, OUTAGE),
    ):
        conn.execute(
            f"UPDATE {table} SET match_status = %s, reason_code = %s WHERE id = %s",
            (status, reason, row),
        )
    conn.commit()
    return world


def _state(conn: Conn, table: str, row_id: UUID) -> tuple[str, str | None]:
    conn.commit()
    row = conn.execute(
        f"SELECT match_status, reason_code FROM {table} WHERE id = %s", (row_id,)
    ).fetchone()
    assert row is not None
    return row["match_status"], row["reason_code"]


def _files(conn: Conn, line: UUID) -> list[UUID]:
    rows = conn.execute(
        "SELECT library_file_id FROM matches WHERE identity_id = %s", (line,)
    ).fetchall()
    return [row["library_file_id"] for row in rows]


def _as_they_were(conn: Conn, world: World, various_line: tuple[str, str | None]) -> None:
    """The listed names and their lines kept their state, and no line of theirs has a match."""
    assert _state(conn, "broadcast_artists", world.spot) == DEFERRED
    assert _state(conn, "broadcast_artists", world.various) == ("auto_matched", None)
    lines = {
        world.spot_deferred: DEFERRED,
        world.spot_pending: ("pending", None),
        world.various_line: various_line,
        world.various_outage: OUTAGE,
    }
    assert {line: _state(conn, "track_identities", line) for line in lines} == lines
    assert {line: _files(conn, line) for line in lines} == {line: [] for line in lines}


def _metallica_matched(conn: Conn, world: World) -> None:
    assert _state(conn, "track_identities", world.metallica_line)[0] == "auto_matched"
    assert _files(conn, world.metallica_line) == [file_named(conn, "one.mp3")]


def test_the_tasks_match_nothing_under_a_listed_name(
    conn: Conn, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artist task's deferred reset passes SPOT BREAK by, and the song stage leaves the
    listed names' pending lines pending, though files of their names and titles are in the
    library."""
    world = _world(conn, client, tmp_path)

    run_artist_matching(monkeypatch, world.playlist, None)
    run_identity_matching(monkeypatch, world.playlist, None)

    _as_they_were(conn, world, ("pending", None))
    _metallica_matched(conn, world)


def test_a_recheck_matches_nothing_under_a_listed_name(
    conn: Conn, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each kind of re-check: enrichment's first (``changed`` with no earlier run: everything),
    a later one whose wave names SPOT BREAK (a Spot Break file indexed since), and Re-run
    Matching (``all``). Each passes the listed names and their lines by: SPOT BREAK stays
    deferred, VARIOUS's line stays in review, and the outage line is not swept. The runs after
    them match none of them."""
    world = _world(conn, client, tmp_path)
    conn.execute(
        "UPDATE track_identities SET match_status = 'needs_review', reason_code = 'NO_LOCAL_FILES'"
        " WHERE id = %s",
        (world.various_line,),
    )
    conn.commit()
    from backend.tasks.matching_recheck_tasks import rematch_undecided_task

    rematch_undecided_task.call_local("changed")
    _as_they_were(conn, world, ("needs_review", "NO_LOCAL_FILES"))
    tagged(tmp_path / "library" / "promo.mp3", "Spot Break", "Promo")
    scan(conn, tmp_path / "library")
    rematch_undecided_task.call_local("changed")
    _as_they_were(conn, world, ("needs_review", "NO_LOCAL_FILES"))
    rematch_undecided_task.call_local("all")
    _as_they_were(conn, world, ("needs_review", "NO_LOCAL_FILES"))
    run_artist_matching(monkeypatch, world.playlist, None)
    run_identity_matching(monkeypatch, world.playlist, None)

    _as_they_were(conn, world, ("needs_review", "NO_LOCAL_FILES"))
    _metallica_matched(conn, world)


def test_the_identity_dry_run_does_as_the_task_does(
    conn: Conn,
    client: TestClient,
    bench: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VARIOUS is resolved (matched) with a pending line: the dry run decides it no line, as the
    task decides none (the dry run reads only the lines of resolved names, so SPOT BREAK's are
    never in it); METALLICA's line it decides as the task then writes."""
    world = _world(conn, client, tmp_path)
    run_artist_matching(monkeypatch, world.playlist, None)  # METALLICA resolved
    dry: Any = load_tool("dry_run_matching")
    out = tmp_path / "identity.json"

    assert dry.main(["--dsn", bench, "--stage", "identity", "--out", str(out)]) == 0
    stage = read_json(out)["sections"]["identity-stage"]
    run_identity_matching(monkeypatch, world.playlist, None)

    assert {str(world.various_line), str(world.various_outage)} & set(stage) == set()
    assert stage[str(world.metallica_line)]["status"] == "auto_matched"
    _as_they_were(conn, world, ("pending", None))
    _metallica_matched(conn, world)

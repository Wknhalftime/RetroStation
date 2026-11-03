"""No run song-matches a line under a listed name (artist model phase 1c, SPEC S6, E3; C7;
Lance, 2026-11-03).

A local act or library files named like a station placeholder (SPOT BREAK) could match its
spot lines to files, and a matched file can reach the stream. So nothing under a name on the
not-an-artist list is song-matched by any run, and its lines keep their state:

- the matching tasks on the name's playlist (artist, then identity): the artist task's
  DEFERRED_RETRY reset passes a listed name by, so SPOT BREAK and its deferred lines stay
  deferred, and the song stage decides no line of a listed name;
- a re-check (Re-run Matching, ``rematch_undecided_task("all")``): its rewind passes listed
  names and their lines by, and the runs after it match none of them;
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


@dataclass(frozen=True)
class World:
    """SPOT BREAK / JINGLE, VARIOUS / MEDLEY and METALLICA / ONE at an AC station, each with a
    library file of its own name and title (spot.mp3, various.mp3, one.mp3). SPOT BREAK and
    VARIOUS are listed."""

    playlist: UUID
    spot: UUID
    various: UUID
    metallica: UUID
    spot_line: UUID
    various_line: UUID
    metallica_line: UUID


def _world(conn: Conn, client: TestClient, tmp_path: Path) -> World:
    root = tmp_path / "library"
    tagged(root / "spot.mp3", "Spot Break", "Jingle")
    tagged(root / "various.mp3", "Various", "Medley")
    tagged(root / "one.mp3", "Metallica", "One")
    scan(conn, root)
    playlist = ingest(
        conn,
        seed.station(conn, "AC"),
        [
            ("SPOT BREAK", "JINGLE", f"{DAY} 08:00:00"),
            ("VARIOUS", "MEDLEY", f"{DAY} 08:04:00"),
            ("METALLICA", "ONE", f"{DAY} 08:08:00"),
        ],
        "listed",
    )
    world = World(
        playlist=playlist,
        spot=name_id(conn, "SPOT BREAK"),
        various=name_id(conn, "VARIOUS"),
        metallica=name_id(conn, "METALLICA"),
        spot_line=line_id(conn, "SPOT BREAK", "JINGLE"),
        various_line=line_id(conn, "VARIOUS", "MEDLEY"),
        metallica_line=line_id(conn, "METALLICA", "ONE"),
    )
    for name in (world.spot, world.various):
        assert client.post(f"/api/v1/matching/artists/{name}/not-an-artist").status_code == 200
    # As on the dev database: SPOT BREAK deferred with its lines (the DEFERRED_RETRY cascade),
    # VARIOUS matched with no link (0045 deleted it).
    for table, row in (("broadcast_artists", world.spot), ("track_identities", world.spot_line)):
        conn.execute(
            f"UPDATE {table} SET match_status = 'needs_review', reason_code = 'DEFERRED_RETRY'"
            " WHERE id = %s",
            (row,),
        )
    conn.execute(
        "UPDATE broadcast_artists SET match_status = 'auto_matched' WHERE id = %s", (world.various,)
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
    assert _state(conn, "broadcast_artists", world.spot) == ("needs_review", "DEFERRED_RETRY")
    assert _state(conn, "track_identities", world.spot_line) == ("needs_review", "DEFERRED_RETRY")
    assert _state(conn, "broadcast_artists", world.various) == ("auto_matched", None)
    assert _state(conn, "track_identities", world.various_line) == various_line
    assert _files(conn, world.spot_line) == []
    assert _files(conn, world.various_line) == []


def _metallica_matched(conn: Conn, world: World) -> None:
    assert _state(conn, "track_identities", world.metallica_line)[0] == "auto_matched"
    assert _files(conn, world.metallica_line) == [file_named(conn, "one.mp3")]


def test_the_tasks_match_nothing_under_a_listed_name(
    conn: Conn, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artist task's deferred reset passes SPOT BREAK by, and the song stage leaves
    VARIOUS's pending line pending, though a file of its name and title is in the library."""
    world = _world(conn, client, tmp_path)

    run_artist_matching(monkeypatch, world.playlist, None)
    run_identity_matching(monkeypatch, world.playlist, None)

    _as_they_were(conn, world, ("pending", None))
    _metallica_matched(conn, world)


def test_a_recheck_matches_nothing_under_a_listed_name(
    conn: Conn, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-run Matching's rewind passes the listed names and their lines by: SPOT BREAK stays
    deferred, and VARIOUS's line stays in review; the runs after it match neither."""
    world = _world(conn, client, tmp_path)
    conn.execute(
        "UPDATE track_identities SET match_status = 'needs_review', reason_code = 'NO_LOCAL_FILES'"
        " WHERE id = %s",
        (world.various_line,),
    )
    conn.commit()
    from backend.tasks.matching_recheck_tasks import rematch_undecided_task

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
    task decides none; METALLICA's line it decides as the task then writes."""
    world = _world(conn, client, tmp_path)
    run_artist_matching(monkeypatch, world.playlist, None)  # METALLICA resolved
    dry: Any = load_tool("dry_run_matching")
    out = tmp_path / "identity.json"

    assert dry.main(["--dsn", bench, "--stage", "identity", "--out", str(out)]) == 0
    stage = read_json(out)["sections"]["identity-stage"]
    run_identity_matching(monkeypatch, world.playlist, None)

    assert str(world.various_line) not in stage
    assert stage[str(world.metallica_line)]["status"] == "auto_matched"
    _as_they_were(conn, world, ("pending", None))
    _metallica_matched(conn, world)

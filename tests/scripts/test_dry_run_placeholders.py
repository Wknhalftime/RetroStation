"""The gate's artist dry run refuses placeholders as the artist matching task does (artist model
phase 1c, SPEC S5, S6, E3; phase 0b's rule that a dry run decides what the task would).

``scripts/dry_run_matching.py --stage artist`` records, for every log name, the decision the
artist stage's engines and refusals make. For a name on the not-an-artist list, and for a name
whose answer is a special-purpose artist, it records the refusal the task then writes
(``needs_review`` / ``NO_CANDIDATES``), so the gate's compare shows what the task will do. The
seeds go through the scan, the ingestion task and the Not an artist route; the task runs after
the dry run, on the same database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from tests.integration import stream_seed as seed
from tests.scripts.gate_world import (
    Conn,
    ingest,
    load_tool,
    name_id,
    read_json,
    run_artist_matching,
    scan,
    tagged,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("queue_in_memory")]

DAY = "1995-03-14"
VARIOUS_ARTISTS = "89ad4ac3-39f7-470e-963a-56509c546377"


@pytest.fixture(scope="module")
def dry() -> Any:
    return load_tool("dry_run_matching")


def _stored(conn: Conn, name: UUID) -> tuple[str, str | None, int]:
    """The name's status, reason and number of links, as the task left them."""
    conn.commit()
    row = conn.execute(
        """SELECT match_status, reason_code,
                  (SELECT count(*) FROM log_name_links l WHERE l.broadcast_artist_id = ba.id)
                      AS links
             FROM broadcast_artists ba WHERE ba.id = %s""",
        (name,),
    ).fetchone()
    assert row is not None
    return row["match_status"], row["reason_code"], int(row["links"])


def test_the_dry_run_refuses_as_the_task_does(
    dry: Any,
    conn: Conn,
    client: TestClient,
    bench: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPOT BREAK, listed, has a local act of its very name; VARIOUS ARTIST scores 96.55
    against Various Artists. The dry run records both as refused, the task writes that and no
    link, and METALLICA is matched by both as before."""
    root = tmp_path / "library"
    tagged(root / "spot.mp3", "Spot Break", "Jingle")
    tagged(root / "one.mp3", "Metallica", "One")
    scan(conn, root)
    conn.execute(
        """INSERT INTO artists (id, name, sort_name, normalized_name, mbid, origin)
           VALUES ('va-1c', 'Various Artists', 'Various Artists', 'various artists', %s,
                   'musicbrainz')""",
        (VARIOUS_ARTISTS,),
    )
    conn.commit()
    playlist = ingest(
        conn,
        seed.station(conn, "AC"),
        [
            ("SPOT BREAK", "JINGLE", f"{DAY} 08:00:00"),
            ("VARIOUS ARTIST", "MEDLEY", f"{DAY} 08:04:00"),
            ("METALLICA", "ONE", f"{DAY} 08:08:00"),
        ],
        "placeholders",
    )
    spot, various, metallica = (
        name_id(conn, n) for n in ("SPOT BREAK", "VARIOUS ARTIST", "METALLICA")
    )
    # The bench database starts empty, the list's seed too: SPOT BREAK is listed by the route.
    assert client.post(f"/api/v1/matching/artists/{spot}/not-an-artist").status_code == 200
    conn.commit()

    out = tmp_path / "artist.json"
    assert dry.main(["--dsn", bench, "--stage", "artist", "--out", str(out)]) == 0
    stage = read_json(out)["sections"]["artist-stage"]
    run_artist_matching(monkeypatch, playlist, None)

    for name in (spot, various):
        entry = stage[str(name)]
        assert (entry["status"], entry["reason"]) == ("needs_review", "NO_CANDIDATES")
        assert _stored(conn, name) == ("needs_review", "NO_CANDIDATES", 0)
    assert (stage[str(metallica)]["status"], stage[str(metallica)]["reason"]) == (
        "auto_matched",
        None,
    )
    assert _stored(conn, metallica) == ("auto_matched", None, 1)

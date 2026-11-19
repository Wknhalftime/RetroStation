"""The Library's merge asks before air play changes (artist model phase 3b PR 3, SPEC D3, Q7,
Q8; design C58, C74; AUD-R079).

``POST /api/v1/library/works/{target}/merge`` keeps its locked request and response bodies and
goes through the guard. A merge whose changes a pin holds merges at once, as today. One that
would change what a line plays where a pin can't hold it answers 409, merging nothing, with
the changes in its detail::

    {"detail": {"message": "...", "changes": [{
        "station_id", "station" (call letters), "identity_id", "title", "artist" (the line as
        logged), "plays" (its plays at that station), "from_file_id", "from_path",
        "to_file_id", "to_path"}, ...]}}

The artist page shows them and asks; Lance's confirm sends
``POST /api/v1/library/works/{target}/merge/approved`` (a sync write route, like the others)
with ``source_work_ids`` and ``approvals``, one ``{"station_id", "identity_id", "file_id"}``
per change he saw (the change's new file). It answers as POST merge does; a change he did not
approve (one found since, or another new file) is refused again, with every change.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.integration import credit_picks_seed as cp
from tests.integration import work_merge_seed as seed
from tests.integration.work_merge_seed import TwoWorks

pytestmark = pytest.mark.integration

WORKS = "/api/v1/library/works"
type Conn = psycopg.Connection[dict]


class Flagged:
    """TwoWorks; the source holds an AC edit as its AC override, so the merge would bring it
    onto the target's line heard twice at an AC station (C74)."""

    def __init__(self, conn: Conn) -> None:
        self.world = TwoWorks(conn)
        self.ac_edit = self.world.source_file(conn)
        cp.override(conn, self.world.source, "AC", self.ac_edit)
        self.ac = seed.station(conn, "AC")
        self.line = seed.heard(conn, self.world.target_master, self.ac, title="Always", times=2)
        conn.commit()

    def body(self, approvals: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        found: dict[str, Any] = {"source_work_ids": [self.world.source]}
        if approvals is not None:
            found["approvals"] = approvals
        return found


def path_of(conn: Conn, file_id: UUID) -> str:
    row = conn.execute("SELECT file_path FROM library_files WHERE id = %s", (file_id,)).fetchone()
    assert row is not None
    return str(row["file_path"])


def call_letters(conn: Conn, station_id: UUID) -> str:
    row = conn.execute("SELECT call_letters FROM stations WHERE id = %s", (station_id,)).fetchone()
    assert row is not None
    return str(row["call_letters"])


def approvals_for(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the artist page sends on confirm: each change's (station, line, new file)."""
    return [
        {"station_id": c["station_id"], "identity_id": c["identity_id"], "file_id": c["to_file_id"]}
        for c in changes
    ]


def test_a_merge_with_no_change_merges_as_today(client: TestClient, db_conn: Conn) -> None:
    """The source's line would move to the target's master; the merge pins it, so nothing that
    plays changes and the merge answers 200 at once."""
    world = TwoWorks(db_conn)
    line = seed.heard(db_conn, world.source_master, seed.station(db_conn), title="Always")
    db_conn.commit()

    response = client.post(
        f"{WORKS}/{world.target}/merge", json={"source_work_ids": [world.source]}
    )

    assert response.status_code == 200
    assert response.json() == {
        "merged_file_count": 2,
        "deleted_work_count": 1,
        "dropped_override_count": 0,
    }
    assert seed.pin_of(db_conn, line) == world.source_master


def test_a_change_needing_approval_is_refused_with_the_changes(
    client: TestClient, db_conn: Conn
) -> None:
    flagged = Flagged(db_conn)
    world = flagged.world

    response = client.post(f"{WORKS}/{world.target}/merge", json=flagged.body())

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert isinstance(detail["message"], str) and detail["message"]
    assert detail["changes"] == [
        {
            "station_id": str(flagged.ac),
            "station": call_letters(db_conn, flagged.ac),
            "identity_id": str(flagged.line),
            "title": "Always",
            "artist": "Somebody",
            "plays": 2,
            "from_file_id": str(world.target_master),
            "from_path": path_of(db_conn, world.target_master),
            "to_file_id": str(flagged.ac_edit),
            "to_path": path_of(db_conn, flagged.ac_edit),
        }
    ]
    assert seed.work_exists(db_conn, world.source)
    assert seed.playing(db_conn, flagged.line) == {(world.target_master, "master")}


def test_the_approved_route_merges_with_those_approvals(client: TestClient, db_conn: Conn) -> None:
    flagged = Flagged(db_conn)
    world = flagged.world
    refused = client.post(f"{WORKS}/{world.target}/merge", json=flagged.body())
    changes = refused.json()["detail"]["changes"]

    response = client.post(
        f"{WORKS}/{world.target}/merge/approved", json=flagged.body(approvals_for(changes))
    )

    assert response.status_code == 200
    assert response.json() == {
        "merged_file_count": 3,
        "deleted_work_count": 1,
        "dropped_override_count": 0,
    }
    assert not seed.work_exists(db_conn, world.source)
    assert seed.playing(db_conn, flagged.line) == {(flagged.ac_edit, "override")}


def test_a_different_change_on_confirm_is_refused_again(client: TestClient, db_conn: Conn) -> None:
    """Between the 409 and the confirm, another target line is heard at the AC station: the
    confirm approves only the change Lance saw, so it is refused again with both."""
    flagged = Flagged(db_conn)
    world = flagged.world
    refused = client.post(f"{WORKS}/{world.target}/merge", json=flagged.body())
    seen = refused.json()["detail"]["changes"]
    newer = seed.heard(db_conn, world.target_master, flagged.ac, title="Always")
    db_conn.commit()

    response = client.post(
        f"{WORKS}/{world.target}/merge/approved", json=flagged.body(approvals_for(seen))
    )

    assert response.status_code == 409
    changes = response.json()["detail"]["changes"]
    assert {c["identity_id"] for c in changes} == {str(flagged.line), str(newer)}
    assert seed.work_exists(db_conn, world.source)

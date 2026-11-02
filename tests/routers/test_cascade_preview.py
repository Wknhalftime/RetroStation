"""The off-air preview of a whole-name Reject or Unmatch (artist model phase 1b, SPEC S5, E1, E2;
M24, AUD-R037).

S5: ``GET /api/v1/matching/artists/{id}/cascade-preview?action=reject|unmatch`` answers
``{off_air: {lines, plays}, kept: {manual, same_name}}``, computed with ``plan_cascade`` (S1):
the lines on air that the action takes off air and their plays, and the lines on air that it
keeps, by why (manual first, then same-name). It writes nothing. The proof runs the action and
compares the preview with what the action did to the lines on air, and with the action's own
answer.

E1: a name with no lines, or none matched, previews 0 / 0, and the action still runs.
E2: the action uses the data as it is when it runs, and answers with its own count, which may
differ from a preview taken before.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.domain.enums import LogNameLinkOrigin, MatchStatus
from tests.routers.cascade_seed import (
    CascadeWorld,
    Conn,
    cascade_body,
    line_row,
    links,
    match_files,
    name_row,
    on_air,
)

METALLICA = "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"
ACTIONS = ["reject", "unmatch"]


@pytest.fixture
def enqueue(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr("backend.routers.matching.identity_matching_task", mock)
    return mock


def _preview(client: TestClient, name_id: UUID, action: str) -> httpx.Response:
    return client.get(
        f"/api/v1/matching/artists/{name_id}/cascade-preview", params={"action": action}
    )


def _act(client: TestClient, name_id: UUID, action: str) -> httpx.Response:
    if action == "reject":
        return client.post(
            f"/api/v1/matching/artists/{name_id}/resolve",
            json={"match_status": "manual_rejected"},
        )
    return client.post(f"/api/v1/matching/artists/{name_id}/unmatch")


def _seed(world: CascadeWorld) -> UUID:
    """A name whose lines are on air for every reason, and some not on air at all."""
    name_id = world.name("CASCADE BAND", MatchStatus.MANUAL_MATCHED)
    world.link(name_id, METALLICA, origin=LogNameLinkOrigin.MANUAL)
    for plays in (30, 12):
        world.line(
            name_id,
            f"SAME {plays}",
            MatchStatus.AUTO_MATCHED,
            file_artist="Cascade Band",
            plays=plays,
        )
    world.line(name_id, "BY HAND", MatchStatus.MANUAL_MATCHED, file_artist="Elsewhere", plays=9)
    world.line(
        name_id, "BY HAND SAME", MatchStatus.MANUAL_MATCHED, file_artist="Cascade Band", plays=1
    )
    for plays in (5, 6, 7):
        world.line(
            name_id,
            f"LINKED {plays}",
            MatchStatus.AUTO_MATCHED,
            file_artist="Metallica",
            plays=plays,
        )
    world.line(name_id, "REVIEW", MatchStatus.NEEDS_REVIEW, file_artist="Cascade Band", plays=8)
    world.line(name_id, "PENDING", MatchStatus.PENDING, plays=4)
    world.line(name_id, "MANUAL REJECTED", MatchStatus.MANUAL_REJECTED, plays=3)
    return name_id


def _expected_from_the_effect(
    before: dict[UUID, tuple[str, int]], after: dict[UUID, tuple[str, int]]
) -> dict[str, Any]:
    """The counts the action's effect on the lines on air implies."""
    gone = set(before) - set(after)
    stayed = set(before) & set(after)
    return cascade_body(
        off_lines=len(gone),
        off_plays=sum(before[line][1] for line in gone),
        manual=sum(1 for line in stayed if before[line][0] == "manual_matched"),
        same_name=sum(1 for line in stayed if before[line][0] == "auto_matched"),
    )


@pytest.mark.parametrize("action", ACTIONS)
def test_preview_matches_the_action(
    client: TestClient, db_conn: Conn, enqueue: MagicMock, action: str
) -> None:
    world = CascadeWorld(db_conn)
    name_id = _seed(world)
    before = on_air(db_conn, name_id)

    preview = _preview(client, name_id, action)
    done = _act(client, name_id, action)

    assert (preview.status_code, done.status_code) == (200, 200)
    expected = _expected_from_the_effect(before, on_air(db_conn, name_id))
    # 3 lines through the link (18 plays) go; 2 same-name and 2 by hand stay.
    assert expected == cascade_body(off_lines=3, off_plays=18, manual=2, same_name=2)
    assert preview.json() == expected
    assert done.json()["cascade"] == expected


@pytest.mark.parametrize("action", ACTIONS)
def test_preview_writes_nothing(
    client: TestClient, db_conn: Conn, enqueue: MagicMock, action: str
) -> None:
    world = CascadeWorld(db_conn)
    name_id = _seed(world)
    lines = [
        row["id"]
        for row in db_conn.execute(
            "SELECT id FROM track_identities WHERE broadcast_artist_id = %s", (name_id,)
        ).fetchall()
    ]

    def state() -> tuple[Any, ...]:
        return (
            name_row(db_conn, name_id),
            {line: (line_row(db_conn, line), match_files(db_conn, line)) for line in lines},
            links(db_conn, name_id),
        )

    before = state()

    assert _preview(client, name_id, action).status_code == 200
    assert _preview(client, name_id, action).status_code == 200
    assert state() == before


@pytest.mark.parametrize("action", ACTIONS)
def test_empty_name(client: TestClient, db_conn: Conn, enqueue: MagicMock, action: str) -> None:
    """E1: no lines, or none matched: 0 / 0, and the action still runs."""
    world = CascadeWorld(db_conn)
    no_lines = world.name("NO LINES", MatchStatus.AUTO_MATCHED)
    none_matched = world.name("NONE MATCHED", MatchStatus.AUTO_MATCHED)
    world.line(none_matched, "PENDING", MatchStatus.PENDING, plays=3)
    world.line(none_matched, "REVIEW", MatchStatus.NEEDS_REVIEW, file_artist="Elsewhere", plays=2)
    zero = cascade_body(0, 0, 0, 0)

    for name_id in (no_lines, none_matched):
        preview = _preview(client, name_id, action)
        done = _act(client, name_id, action)

        assert (preview.status_code, preview.json()) == (200, zero)
        assert (done.status_code, done.json()["cascade"]) == (200, zero)
        expected = "manual_rejected" if action == "reject" else "needs_review"
        assert name_row(db_conn, name_id)["match_status"] == expected


@pytest.mark.parametrize("action", ACTIONS)
def test_action_reports_its_own_count(
    client: TestClient, db_conn: Conn, enqueue: MagicMock, action: str
) -> None:
    """E2: the data changes between the preview and the confirm; the action does what the data
    says when it runs and answers with that count, not the preview's."""
    world = CascadeWorld(db_conn)
    name_id = world.name("CASCADE BAND", MatchStatus.AUTO_MATCHED)
    linked = world.line(
        name_id, "LINKED", MatchStatus.AUTO_MATCHED, file_artist="Metallica", plays=5
    )
    world.line(name_id, "SAME", MatchStatus.AUTO_MATCHED, file_artist="Cascade Band", plays=10)
    preview = _preview(client, name_id, action)
    assert preview.json() == cascade_body(1, 5, manual=0, same_name=1)
    # Meanwhile: a song is matched through the link, and the curator matches one by hand.
    late = world.line(name_id, "LATE", MatchStatus.AUTO_MATCHED, file_artist="Metallica", plays=2)
    db_conn.execute(
        "UPDATE track_identities SET match_status = 'manual_matched' WHERE id = %s", (linked.id,)
    )
    db_conn.commit()

    done = _act(client, name_id, action)

    assert done.status_code == 200
    assert done.json()["cascade"] == cascade_body(1, 2, manual=1, same_name=1)
    assert line_row(db_conn, linked.id)["match_status"] == "manual_matched"
    assert line_row(db_conn, late.id)["match_status"] != "auto_matched"


def test_unknown_name(client: TestClient) -> None:
    assert _preview(client, uuid4(), "reject").status_code == 404


@pytest.mark.parametrize("query", [{"action": "delete"}, {"action": ""}, {}])
def test_the_action_is_reject_or_unmatch(
    client: TestClient, db_conn: Conn, query: dict[str, str]
) -> None:
    name_id = CascadeWorld(db_conn).name()
    response = client.get(f"/api/v1/matching/artists/{name_id}/cascade-preview", params=query)
    assert response.status_code == 422

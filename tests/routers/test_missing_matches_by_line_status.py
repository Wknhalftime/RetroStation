"""The station missing-matches report counts a line by its own status (artist model phase 1b,
SPEC S7; C37, AUD-R038).

``GET /api/v1/stations/{id}/reports/missing-matches`` lists a line when its own status is
unresolved (pending, needs_review, auto_rejected, manual_rejected), whatever its name's status:
a matched line under an unresolved name is not missing, and an unresolved line under a matched
name is. Before 1b a line was also listed when its name was unresolved; after a safe Unmatch
(S3) the name is ``needs_review`` while its kept lines still play, so they would have shown as
missing. The total, the items and the impact share follow the same rule.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.domain.enums import MatchStatus
from tests.routers.cascade_seed import CascadeWorld, Conn

UNRESOLVED = [
    MatchStatus.PENDING,
    MatchStatus.NEEDS_REVIEW,
    MatchStatus.AUTO_REJECTED,
    MatchStatus.MANUAL_REJECTED,
]
MATCHED = [MatchStatus.AUTO_MATCHED, MatchStatus.MANUAL_MATCHED]


@pytest.fixture
def enqueue(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr("backend.routers.matching.identity_matching_task", mock)
    return mock


def _report(client: TestClient, station_id: UUID) -> dict[str, Any]:
    response = client.get(
        f"/api/v1/stations/{station_id}/reports/missing-matches", params={"limit": 500}
    )
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def _listed(body: dict[str, Any]) -> set[str]:
    return {item["identity_id"] for item in body["items"]}


@pytest.mark.parametrize("name_status", UNRESOLVED)
@pytest.mark.parametrize("line_status", MATCHED)
def test_a_matched_line_under_an_unresolved_name_is_not_missing(
    client: TestClient, db_conn: Conn, name_status: MatchStatus, line_status: MatchStatus
) -> None:
    world = CascadeWorld(db_conn)
    name_id = world.name("UNRESOLVED NAME", name_status)
    world.line(name_id, "STILL PLAYS", line_status, file_artist="Unresolved Name", plays=6)

    body = _report(client, world.station_id)

    assert (body["total"], body["items"]) == (0, [])


@pytest.mark.parametrize("name_status", MATCHED)
@pytest.mark.parametrize("line_status", UNRESOLVED)
def test_an_unresolved_line_under_a_matched_name_is_missing(
    client: TestClient, db_conn: Conn, name_status: MatchStatus, line_status: MatchStatus
) -> None:
    world = CascadeWorld(db_conn)
    name_id = world.name("MATCHED NAME", name_status)
    line = world.line(name_id, "NOT MATCHED", line_status, plays=2)

    body = _report(client, world.station_id)

    assert body["total"] == 1
    [item] = body["items"]
    assert (item["identity_id"], item["track_status"], item["play_count"]) == (
        str(line.id),
        line_status.value,
        2,
    )


def test_total_items_and_impact_follow_the_line(client: TestClient, db_conn: Conn) -> None:
    world = CascadeWorld(db_conn)
    in_review = world.name("IN REVIEW", MatchStatus.NEEDS_REVIEW)
    matched = world.name("MATCHED", MatchStatus.AUTO_MATCHED)
    world.line(in_review, "PLAYS ON", MatchStatus.AUTO_MATCHED, file_artist="In Review", plays=50)
    missing_a = world.line(in_review, "MISSING A", MatchStatus.NEEDS_REVIEW, plays=3)
    missing_b = world.line(matched, "MISSING B", MatchStatus.PENDING, plays=1)
    world.line(matched, "FINE", MatchStatus.MANUAL_MATCHED, file_artist="Elsewhere", plays=9)

    body = _report(client, world.station_id)

    assert body["total"] == 2
    assert _listed(body) == {str(missing_a.id), str(missing_b.id)}
    impact = {item["identity_id"]: item["impact_pct"] for item in body["items"]}
    # The share is of the listed lines' plays only (4), not of the line still playing.
    assert impact == {str(missing_a.id): 75.0, str(missing_b.id): 25.0}


def test_after_a_safe_unmatch_the_kept_lines_are_not_missing(
    client: TestClient, db_conn: Conn, enqueue: MagicMock
) -> None:
    """S3 with S7: the name goes to needs_review; its kept lines still play and are not
    listed; the lines it reopened are."""
    world = CascadeWorld(db_conn)
    name_id = world.name("CASCADE BAND", MatchStatus.AUTO_MATCHED)
    same_name = world.line(
        name_id, "SAME", MatchStatus.AUTO_MATCHED, file_artist="Cascade Band", plays=20
    )
    by_hand = world.line(
        name_id, "BY HAND", MatchStatus.MANUAL_MATCHED, file_artist="Other", plays=4
    )
    through_link = world.line(
        name_id, "LINKED", MatchStatus.AUTO_MATCHED, file_artist="Another", plays=3
    )
    assert _report(client, world.station_id)["total"] == 0

    assert client.post(f"/api/v1/matching/artists/{name_id}/unmatch").status_code == 200

    body = _report(client, world.station_id)
    assert _listed(body) == {str(through_link.id)}
    assert body["total"] == 1
    assert not {str(same_name.id), str(by_hand.id)} & _listed(body)

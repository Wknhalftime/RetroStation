"""A whole-name Reject and Unmatch keep manual and same-name lines on air (artist model phase 1b,
SPEC S2, S3, E3, E5; M24, C19, AUD-R037).

S2, Reject (``POST /matching/artists/{id}/resolve`` with ``manual_rejected``): the name becomes
``manual_rejected``; every one of its links becomes ``rejected`` (C19); the kept lines (manual
ones, and auto-matched ones whose match is to a same-name file; ``plan_cascade``, S1) are
untouched; every other line becomes ``auto_rejected`` as today (status only: its tier, reason
and match rows stay, as today's cascade left them).

S3, Unmatch (``POST /matching/artists/{id}/unmatch``): the name becomes ``needs_review`` with
``USER_UNMATCHED``; its links become ``rejected``; kept lines keep their status and song
matches; every other line becomes ``needs_review`` with ``USER_UNMATCHED`` (tier and detail
cleared) and its song matches are deleted. The locked ``test_matching_unmatch.py`` proves S3
too; this file adds every kind of line.

Both answer with the real counts of what they did (E2; the preview's shape, S5). E3: a second
Reject changes nothing (a second Unmatch is a 409, as today: ``test_matching_unmatch.py``). E5:
every link of the name becomes rejected, whatever its state and origin. A database error rolls
the whole request back, as today.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.domain.enums import LogNameLinkOrigin, LogNameLinkState, MatchStatus
from tests.routers.cascade_seed import (
    CascadeWorld,
    Conn,
    Line,
    cascade_body,
    line_row,
    links,
    match_files,
    name_row,
)

METALLICA = "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"
MEGADETH = "a9044915-8be3-4c7e-b11f-9e2d2ea0a91e"
SLAYER = "bdacc37b-8633-4bf8-9dd5-4662ee651aec"
ANTHRAX = "0ff1b3a6-1b5c-4ad0-9d8e-0ebdc1b3a6a1"


@pytest.fixture
def enqueue(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr("backend.routers.matching.identity_matching_task", mock)
    return mock


def _reject(client: TestClient, name_id: UUID) -> httpx.Response:
    return client.post(
        f"/api/v1/matching/artists/{name_id}/resolve", json={"match_status": "manual_rejected"}
    )


def _unmatch(client: TestClient, name_id: UUID) -> httpx.Response:
    return client.post(f"/api/v1/matching/artists/{name_id}/unmatch")


class _Name:
    """CASCADE BAND with one line of every kind; the plays make each count distinct."""

    def __init__(self, world: CascadeWorld, status: MatchStatus) -> None:
        self.id = world.name("CASCADE BAND", status)
        self.metallica = world.link(self.id, METALLICA, origin=LogNameLinkOrigin.MANUAL)
        self.megadeth = world.link(self.id, MEGADETH, state=LogNameLinkState.SUGGESTED)
        line = world.line
        # Kept.
        self.same_name = line(
            self.id, "SAME NAME", MatchStatus.AUTO_MATCHED, file_artist="Cascade Band", plays=50
        )
        self.manual = line(
            self.id,
            "BY HAND",
            MatchStatus.MANUAL_MATCHED,
            file_artist="Other Band",
            plays=7,
            tier="manual",
        )
        self.manual_rejected = line(
            self.id,
            "REJECTED BY HAND",
            MatchStatus.MANUAL_REJECTED,
            plays=3,
            tier=None,
            reason_code="USER_UNMATCHED",
        )
        # Changed: on air before.
        self.through_link = line(
            self.id,
            "THROUGH THE LINK",
            MatchStatus.AUTO_MATCHED,
            file_artist="Another Act",
            plays=4,
        )
        # Changed: not on air before.
        self.in_review = line(
            self.id,
            "IN REVIEW",
            MatchStatus.NEEDS_REVIEW,
            file_artist="Cascade Band",
            plays=2,
            reason_code="LOW_CONFIDENCE",
            reason_detail="Score 70",
        )
        self.pending = line(self.id, "PENDING", MatchStatus.PENDING, plays=1, tier=None)
        self.auto_rejected = line(
            self.id,
            "AUTO REJECTED",
            MatchStatus.AUTO_REJECTED,
            plays=1,
            reason_code="NO_LOCAL_FILES",
        )

    @property
    def kept(self) -> tuple[Line, ...]:
        return (self.same_name, self.manual, self.manual_rejected)

    @property
    def changed(self) -> tuple[Line, ...]:
        return (self.through_link, self.in_review, self.pending, self.auto_rejected)


def _snapshot(conn: Conn, lines: tuple[Line, ...]) -> dict[UUID, tuple[dict[str, Any], list[Any]]]:
    return {line.id: (line_row(conn, line.id), match_files(conn, line.id)) for line in lines}


# --- S2: Reject ----------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [MatchStatus.NEEDS_REVIEW, MatchStatus.AUTO_MATCHED])
def test_reject_keeps_same_name_and_manual(
    client: TestClient, db_conn: Conn, enqueue: MagicMock, status: MatchStatus
) -> None:
    world = CascadeWorld(db_conn)
    name = _Name(world, status)
    kept_before = _snapshot(db_conn, name.kept)
    changed_before = _snapshot(db_conn, name.changed)

    response = _reject(client, name.id)

    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["match_status"]) == (str(name.id), "manual_rejected")
    assert body["cascade"] == cascade_body(off_lines=1, off_plays=4, manual=1, same_name=1)
    assert name_row(db_conn, name.id)["match_status"] == "manual_rejected"
    # The kept lines are untouched: status, tier, reason and matches.
    assert _snapshot(db_conn, name.kept) == kept_before
    # Every other line is auto_rejected, as today: only its status changes.
    for line in name.changed:
        row, matches = changed_before[line.id]
        assert line_row(db_conn, line.id) == {**row, "match_status": "auto_rejected"}
        assert match_files(db_conn, line.id) == matches
    assert {artist: link["state"] for artist, link in links(db_conn, name.id).items()} == {
        name.metallica: "rejected",
        name.megadeth: "rejected",
    }
    enqueue.assert_not_called()


def test_reject_twice(client: TestClient, db_conn: Conn, enqueue: MagicMock) -> None:
    """E3: the second press changes nothing and takes nothing more off air: not even a
    rejected link's ``updated_at`` (a link already rejected is left as it is)."""
    world = CascadeWorld(db_conn)
    name = _Name(world, MatchStatus.NEEDS_REVIEW)
    first = _reject(client, name.id)
    after_first = (
        name_row(db_conn, name.id),
        _snapshot(db_conn, name.kept + name.changed),
        links(db_conn, name.id),
    )

    second = _reject(client, name.id)

    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json()["match_status"] == "manual_rejected"
    assert second.json()["cascade"] == cascade_body(0, 0, manual=1, same_name=1)
    assert (
        name_row(db_conn, name.id),
        _snapshot(db_conn, name.kept + name.changed),
        links(db_conn, name.id),
    ) == after_first


# --- S3: Unmatch ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        MatchStatus.AUTO_MATCHED,
        MatchStatus.MANUAL_MATCHED,
        MatchStatus.AUTO_REJECTED,
        MatchStatus.MANUAL_REJECTED,
    ],
)
def test_unmatch_keeps_same_name_and_manual(
    client: TestClient, db_conn: Conn, enqueue: MagicMock, status: MatchStatus
) -> None:
    world = CascadeWorld(db_conn)
    name = _Name(world, status)
    kept_before = _snapshot(db_conn, name.kept)

    response = _unmatch(client, name.id)

    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["match_status"]) == (str(name.id), "needs_review")
    assert body["cascade"] == cascade_body(off_lines=1, off_plays=4, manual=1, same_name=1)
    assert name_row(db_conn, name.id) == {
        "match_status": "needs_review",
        "reason_code": "USER_UNMATCHED",
        "reason_detail": None,
    }
    assert _snapshot(db_conn, name.kept) == kept_before
    for line in name.changed:
        assert line_row(db_conn, line.id) == {
            "match_status": "needs_review",
            "match_tier": None,
            "reason_code": "USER_UNMATCHED",
            "reason_detail": None,
        }
        assert match_files(db_conn, line.id) == []
    assert {artist: link["state"] for artist, link in links(db_conn, name.id).items()} == {
        name.metallica: "rejected",
        name.megadeth: "rejected",
    }


# --- E5: every link -------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["reject", "unmatch"])
def test_all_links_rejected(
    client: TestClient, db_conn: Conn, enqueue: MagicMock, action: str
) -> None:
    """E5: links in every state and of every origin become rejected; none is added or removed,
    and each keeps its origin and strength."""
    world = CascadeWorld(db_conn)
    name_id = world.name("BIG FOUR", MatchStatus.MANUAL_MATCHED)
    world.link(name_id, METALLICA, origin=LogNameLinkOrigin.MANUAL, strength=100)
    world.link(name_id, MEGADETH, origin=LogNameLinkOrigin.AUTOMATIC, strength=88)
    world.link(name_id, SLAYER, origin=LogNameLinkOrigin.LEGACY, state=LogNameLinkState.SUGGESTED)
    world.link(name_id, ANTHRAX, state=LogNameLinkState.REJECTED, strength=60)
    other = world.name("METALLICA", MatchStatus.AUTO_MATCHED)
    world.link(other, METALLICA)
    before = links(db_conn, name_id)

    response = _reject(client, name_id) if action == "reject" else _unmatch(client, name_id)

    assert response.status_code == 200
    after = links(db_conn, name_id)
    assert set(after) == set(before)
    for artist, link in after.items():
        assert link["state"] == "rejected"
        assert (link["origin"], link["strength"], link["id"]) == (
            before[artist]["origin"],
            before[artist]["strength"],
            before[artist]["id"],
        )
    # Another name's link to the same artist is not touched.
    [other_link] = links(db_conn, other).values()
    assert other_link["state"] == "active"


def test_a_name_with_no_links_is_rejected(
    client: TestClient, db_conn: Conn, enqueue: MagicMock
) -> None:
    world = CascadeWorld(db_conn)
    name_id = world.name("NO LINKS", MatchStatus.NEEDS_REVIEW)

    response = _reject(client, name_id)

    assert response.status_code == 200
    assert links(db_conn, name_id) == {}
    assert name_row(db_conn, name_id)["match_status"] == "manual_rejected"


# --- one transaction -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "table", "operation"),
    [
        ("reject", "log_name_links", "UPDATE"),
        ("reject", "track_identities", "UPDATE"),
        ("unmatch", "log_name_links", "UPDATE"),
        ("unmatch", "matches", "DELETE"),
    ],
)
def test_a_failed_write_leaves_nothing(
    client: TestClient,
    db_conn: Conn,
    enqueue: MagicMock,
    action: str,
    table: str,
    operation: str,
) -> None:
    """A write the action makes fails at the database: the name, its lines, their matches and
    its links are all as they were (the request is rolled back whole, as today)."""
    world = CascadeWorld(db_conn)
    name = _Name(world, MatchStatus.MANUAL_MATCHED)
    before = (
        name_row(db_conn, name.id),
        _snapshot(db_conn, name.kept + name.changed),
        links(db_conn, name.id),
    )
    db_conn.execute(
        """CREATE OR REPLACE FUNCTION test_refuse_write() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN RAISE EXCEPTION 'write refused'; END $$"""
    )
    db_conn.execute(
        f"CREATE TRIGGER test_refuse_write BEFORE {operation} ON {table}"
        " FOR EACH ROW EXECUTE FUNCTION test_refuse_write()"
    )
    db_conn.commit()
    try:
        response = _reject(client, name.id) if action == "reject" else _unmatch(client, name.id)
    finally:
        db_conn.execute(f"DROP TRIGGER IF EXISTS test_refuse_write ON {table}")
        db_conn.execute("DROP FUNCTION IF EXISTS test_refuse_write()")
        db_conn.commit()

    assert response.status_code == 500
    assert (
        name_row(db_conn, name.id),
        _snapshot(db_conn, name.kept + name.changed),
        links(db_conn, name.id),
    ) == before

"""Not an artist: the not-an-artist list and its routes (artist model phase 1c, SPEC S6, S9, E4;
C7, C75, AUD-R037).

``log_name_placeholders`` (migration 0045) lists the log names that are not artists by their
exact cleaned name, seeded with the five station names (SPOT BREAK, SPECIAL PROGRAMMING,
SYNDICATED PROGRAM, VARIOUS, VARIOUS ARTISTS). A listed name:

- is never linked: ``POST /api/v1/matching/artists/{id}/not-an-artist`` lists it (added by
  ``curator``) and removes its links in every state; Resolve to an artist answers 409, and
  nothing is written. The matcher never matches it (``tests/services/
  test_placeholder_links_refused.py``);
- keeps its status, because the list hides it (S3), and its lines are untouched;
- is hidden from the Resolution Center queue, in every view, with its count;
- has its lines left out of the station missing-matches report: the total, the items and the
  impact share.

``DELETE`` takes the name off the list again, and the name takes its status from the links it
holds (S3's rule): a matched name with none goes to ``needs_review`` with ``LINK_REMOVED``.
Both are sync routes on ``SyncRepos`` (C75) and answer 404 for a name that does not exist.

E4: pressing Not an artist twice is one listing, and taking a name off twice is harmless. An
Undo after its toast is gone goes through the queue's placeholder view
(``placeholders=true``), which shows every listed name, whatever its status and likelihood.

Resolve to a special-purpose artist (``SPECIAL_PURPOSE_MBIDS``, S5) answers 422 and writes
nothing, not even a stub artist.

The table is not one the test fixture truncates, so every test here leaves it as seeded.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.dependencies import get_db_connection, get_sync_repos
from backend.domain.enums import LogNameLinkOrigin, LogNameLinkState, MatchStatus
from backend.routers.matching import router
from tests.routers.cascade_seed import CascadeWorld, Conn, line_row, links, match_files, name_row

VARIOUS_ARTISTS = "89ad4ac3-39f7-470e-963a-56509c546377"
UNKNOWN = "125ec42a-7229-4250-afc5-e057484327fe"
SEEDED = {
    "spot break",
    "special programming",
    "syndicated program",
    "various",
    "various artists",
}
QUEUE = "/api/v1/matching/queue"


def _url(name_id: UUID) -> str:
    return f"/api/v1/matching/artists/{name_id}/not-an-artist"


@pytest.fixture(autouse=True)
def _list_as_seeded(db_conn: Conn) -> Iterator[None]:
    """The list holds the five seeded names and nothing else, before and after each test."""

    def reset() -> None:
        db_conn.rollback()
        db_conn.execute("DELETE FROM log_name_placeholders WHERE added_by = 'curator'")
        for name in sorted(SEEDED):
            db_conn.execute(
                "INSERT INTO log_name_placeholders (normalized_name, added_by)"
                " VALUES (%s, 'migration') ON CONFLICT (normalized_name) DO NOTHING",
                (name,),
            )
        db_conn.commit()

    reset()
    yield
    reset()


@pytest.fixture
def enqueue(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr("backend.routers.matching.identity_matching_task", mock)
    return mock


# --- reads ---------------------------------------------------------------------------------------


def _list(conn: Conn) -> dict[str, str]:
    """The list: normalized name -> who added it."""
    conn.commit()
    rows = conn.execute("SELECT normalized_name, added_by FROM log_name_placeholders").fetchall()
    return {row["normalized_name"]: row["added_by"] for row in rows}


def _queue(client: TestClient, **params: str) -> dict[str, Any]:
    response = client.get(QUEUE, params={"limit": "500", **params})
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def _queued(client: TestClient, **params: str) -> set[str]:
    return {item["id"] for item in _queue(client, **params)["items"]}


def _report(client: TestClient, station_id: UUID) -> dict[str, Any]:
    response = client.get(
        f"/api/v1/stations/{station_id}/reports/missing-matches", params={"limit": 500}
    )
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def _missing(client: TestClient, station_id: UUID) -> set[str]:
    return {item["identity_id"] for item in _report(client, station_id)["items"]}


def _snapshot(conn: Conn, name_id: UUID, line_ids: list[UUID]) -> dict[str, Any]:
    """The name's row and its lines' rows and match files, to compare before and after."""
    conn.commit()
    return {
        "name": name_row(conn, name_id),
        "lines": {line_id: line_row(conn, line_id) for line_id in line_ids},
        "files": {line_id: match_files(conn, line_id) for line_id in line_ids},
    }


# --- POST ----------------------------------------------------------------------------------------


def test_not_an_artist_lists_the_name_and_removes_its_links(
    client: TestClient, db_conn: Conn
) -> None:
    """Every link goes, whatever its state; the name keeps its status, its lines and their
    matches are untouched."""
    world = CascadeWorld(db_conn)
    name_id = world.name("ZZ TRAFFIC AND WEATHER", MatchStatus.AUTO_MATCHED)
    world.link(name_id, "mbid-active")
    world.link(name_id, "mbid-suggested", state=LogNameLinkState.SUGGESTED, strength=70)
    world.link(
        name_id, "mbid-rejected", origin=LogNameLinkOrigin.MANUAL, state=LogNameLinkState.REJECTED
    )
    playing = world.line(
        name_id, "WEATHER", MatchStatus.AUTO_MATCHED, file_artist="Somebody", plays=3
    )
    waiting = world.line(name_id, "TRAFFIC", MatchStatus.NEEDS_REVIEW, plays=2)
    before = _snapshot(db_conn, name_id, [playing.id, waiting.id])

    response = client.post(_url(name_id))

    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["listed"]) == (str(name_id), True)
    seeded = dict.fromkeys(SEEDED, "migration")
    assert _list(db_conn) == {**seeded, "zz traffic and weather": "curator"}
    assert links(db_conn, name_id) == {}
    assert _snapshot(db_conn, name_id, [playing.id, waiting.id]) == before


def test_a_listed_name_leaves_the_queue(client: TestClient, db_conn: Conn) -> None:
    """A suggested name the default view shows leaves every view, and the counts."""
    world = CascadeWorld(db_conn)
    name_id = world.name("ZZ NOT A BAND", MatchStatus.NEEDS_REVIEW)
    world.link(name_id, "mbid-guess", state=LogNameLinkState.SUGGESTED, strength=72)
    world.line(name_id, "JINGLE", MatchStatus.PENDING, plays=4)
    assert str(name_id) in _queued(client, include_unlikely="false")

    assert client.post(_url(name_id)).status_code == 200

    for view in ({"include_unlikely": "false"}, {"include_unlikely": "true"}):
        body = _queue(client, **view)
        assert str(name_id) not in {item["id"] for item in body["items"]}
        assert (body["total"], body["unlikely_total"]) == (0, 0)
    assert _queued(client, include_unlikely="true", search="NOT A BAND") == set()


def test_a_listed_names_lines_leave_the_missing_matches_report(
    client: TestClient, db_conn: Conn
) -> None:
    world = CascadeWorld(db_conn)
    listed = world.name("ZZ STATION ID", MatchStatus.NEEDS_REVIEW)
    real = world.name("ZZ REAL BAND", MatchStatus.AUTO_MATCHED)
    gone = world.line(listed, "ID", MatchStatus.NEEDS_REVIEW, plays=30)
    stays = world.line(real, "SONG", MatchStatus.PENDING, plays=10)
    assert _missing(client, world.station_id) == {str(gone.id), str(stays.id)}

    assert client.post(_url(listed)).status_code == 200

    body = _report(client, world.station_id)
    assert body["total"] == 1
    [item] = body["items"]
    # The share is of the lines still listed: the listed name's 30 plays no longer count.
    assert (item["identity_id"], item["play_count"], item["impact_pct"]) == (
        str(stays.id),
        10,
        100.0,
    )


@pytest.mark.parametrize(
    ("spelling", "status"),
    [
        ("VARIOUS", MatchStatus.AUTO_MATCHED),
        ("VARIOUS ARTISTS", MatchStatus.AUTO_MATCHED),
        ("SPOT BREAK", MatchStatus.NEEDS_REVIEW),
        ("SPECIAL PROGRAMMING", MatchStatus.MANUAL_REJECTED),
        ("SYNDICATED PROGRAM", MatchStatus.PENDING),
    ],
)
def test_the_station_placeholders_are_hidden(
    client: TestClient, db_conn: Conn, spelling: str, status: MatchStatus
) -> None:
    """The five seeded names, as they are on the dev database (VARIOUS and VARIOUS ARTISTS kept
    ``auto_matched`` with no link): out of the queue and of the missing-matches report, while
    a line in review would bring them in."""
    world = CascadeWorld(db_conn)
    name_id = world.name(spelling, status)
    review = world.line(name_id, "A LINE", MatchStatus.NEEDS_REVIEW, plays=12)
    other = world.name("ZZ SOMEBODY", MatchStatus.AUTO_MATCHED)
    other_line = world.line(other, "B LINE", MatchStatus.NEEDS_REVIEW, plays=3)

    body = _queue(client, include_unlikely="true")
    assert {item["id"] for item in body["items"]} == {str(other)}
    # A page with rows counts as the empty page does: only ZZ SOMEBODY, which is not likely.
    assert (body["total"], body["unlikely_total"]) == (1, 1)
    assert _missing(client, world.station_id) == {str(other_line.id)}
    assert _report(client, world.station_id)["total"] == 1
    assert str(review.id) not in _missing(client, world.station_id)


# --- the placeholder view and DELETE (E4) --------------------------------------------------------


def test_the_placeholder_view_shows_every_listed_name(client: TestClient, db_conn: Conn) -> None:
    """``placeholders=true`` lists the listed names and only them, whatever their status and
    whether they would count as likely; search narrows it."""
    world = CascadeWorld(db_conn)
    listed = {
        world.name("VARIOUS", MatchStatus.AUTO_MATCHED),
        world.name("VARIOUS ARTISTS", MatchStatus.AUTO_MATCHED),
        world.name("SPECIAL PROGRAMMING", MatchStatus.MANUAL_REJECTED),
        world.name("SPOT BREAK", MatchStatus.NEEDS_REVIEW),
    }
    world.name("ZZ IN REVIEW", MatchStatus.NEEDS_REVIEW)

    for likely in ("false", "true"):
        body = _queue(client, placeholders="true", include_unlikely=likely)
        assert {item["id"] for item in body["items"]} == {str(n) for n in listed}
        assert body["total"] == len(listed)
    assert len(_queued(client, placeholders="true", search="various")) == 2


def test_off_the_list_again(client: TestClient, db_conn: Conn) -> None:
    """DELETE takes the name off the list; it takes its status from its links (none here): a
    matched name goes to review with LINK_REMOVED, and the queue has it again."""
    world = CascadeWorld(db_conn)
    name_id = world.name("ZZ WAS LISTED", MatchStatus.AUTO_MATCHED)
    world.link(name_id, "mbid-active")
    world.line(name_id, "A SONG", MatchStatus.AUTO_MATCHED, file_artist="Elsewhere", plays=2)
    assert client.post(_url(name_id)).status_code == 200
    assert str(name_id) in _queued(client, placeholders="true")

    response = client.delete(_url(name_id))

    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["listed"]) == (str(name_id), False)
    assert "zz was listed" not in _list(db_conn)
    db_conn.commit()
    state = name_row(db_conn, name_id)
    assert (state["match_status"], state["reason_code"]) == ("needs_review", "LINK_REMOVED")
    assert str(name_id) in _queued(client, include_unlikely="true")
    assert str(name_id) not in _queued(client, placeholders="true")


def test_off_the_list_a_name_in_review_keeps_its_status(client: TestClient, db_conn: Conn) -> None:
    world = CascadeWorld(db_conn)
    name_id = world.name("ZZ STILL IN REVIEW", MatchStatus.NEEDS_REVIEW)
    assert client.post(_url(name_id)).status_code == 200
    before = _snapshot(db_conn, name_id, [])

    assert client.delete(_url(name_id)).status_code == 200

    assert _snapshot(db_conn, name_id, []) == before


def test_twice(client: TestClient, db_conn: Conn) -> None:
    """E4: Not an artist pressed twice is one listing, the first one's; taking the name off
    twice leaves it off. An Undo after the toast is gone is the same DELETE, from the
    placeholder view."""
    world = CascadeWorld(db_conn)
    name_id = world.name("ZZ PRESSED TWICE", MatchStatus.AUTO_MATCHED)
    world.link(name_id, "mbid-active")

    first, second = client.post(_url(name_id)), client.post(_url(name_id))

    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json()["listed"] is True
    listed = {name: by for name, by in _list(db_conn).items() if name not in SEEDED}
    assert listed == {"zz pressed twice": "curator"}
    assert links(db_conn, name_id) == {}
    db_conn.commit()
    assert name_row(db_conn, name_id)["match_status"] == "auto_matched"

    assert str(name_id) in _queued(client, placeholders="true")  # the Undo, later
    first, second = client.delete(_url(name_id)), client.delete(_url(name_id))

    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json()["listed"] is False
    assert "zz pressed twice" not in _list(db_conn)
    db_conn.commit()
    state = name_row(db_conn, name_id)
    assert (state["match_status"], state["reason_code"]) == ("needs_review", "LINK_REMOVED")


def test_an_unknown_name_is_404(client: TestClient, db_conn: Conn) -> None:
    """The name's own 404, which names it (not the router's for a path it lacks)."""
    unknown = uuid4()

    for response in (client.post(_url(unknown)), client.delete(_url(unknown))):
        assert response.status_code == 404
        assert str(unknown) in response.json()["detail"]
    assert set(_list(db_conn)) == SEEDED


# --- Resolve refuses (S5, S6) --------------------------------------------------------------------


def _resolve(client: TestClient, name_id: UUID, target: str) -> int:
    """Resolve to *target*; the status, with the refusal's own detail (a string, where a
    request the model rejects gets a list)."""
    response = client.post(
        f"/api/v1/matching/artists/{name_id}/resolve",
        json={"match_status": "manual_matched", "target_artist_id": target},
    )
    if response.status_code >= 400:
        assert isinstance(response.json()["detail"], str), response.text
    return response.status_code


def _stubs_of(conn: Conn, mbid: str) -> int:
    conn.commit()
    row = conn.execute("SELECT count(*) AS n FROM artists WHERE mbid = %s", (mbid,)).fetchone()
    assert row is not None
    return int(row["n"])


def _artists(conn: Conn) -> int:
    conn.commit()
    row = conn.execute("SELECT count(*) AS n FROM artists").fetchone()
    assert row is not None
    return int(row["n"])


def test_resolve_refuses_a_listed_name(
    client: TestClient, db_conn: Conn, enqueue: MagicMock
) -> None:
    world = CascadeWorld(db_conn)
    name_id = world.name("SPOT BREAK", MatchStatus.NEEDS_REVIEW)
    world.line(name_id, "A LINE", MatchStatus.NEEDS_REVIEW)
    before = _snapshot(db_conn, name_id, [])
    artists = _artists(db_conn)

    assert _resolve(client, name_id, "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab") == 409

    assert _snapshot(db_conn, name_id, []) == before
    assert links(db_conn, name_id) == {}
    assert _artists(db_conn) == artists  # no stub for the MBID
    enqueue.assert_not_called()


def _catalog_artist(conn: Conn, name: str, mbid: str) -> str:
    artist_id = str(uuid4())
    conn.execute(
        "INSERT INTO artists (id, name, sort_name, mbid, origin, needs_enhancement)"
        " VALUES (%s, %s, %s, %s, 'musicbrainz', FALSE)",
        (artist_id, name, name, mbid),
    )
    conn.commit()
    return artist_id


@pytest.mark.parametrize("by_id", [False, True], ids=["by MBID", "by artist id"])
def test_resolve_refuses_a_placeholder_artist(
    client: TestClient, db_conn: Conn, enqueue: MagicMock, by_id: bool
) -> None:
    """S5: Resolve to Various Artists or [unknown], by its MBID or by the catalog artist that
    holds it, answers 422; the name and its links are as they were, and no stub is made."""
    world = CascadeWorld(db_conn)
    name_id = world.name("ZZ COMPILATION", MatchStatus.NEEDS_REVIEW)
    unknown = _catalog_artist(db_conn, "[unknown]", UNKNOWN)
    target = unknown if by_id else VARIOUS_ARTISTS
    before = _snapshot(db_conn, name_id, [])
    artists = _artists(db_conn)

    assert _resolve(client, name_id, target) == 422

    assert _snapshot(db_conn, name_id, []) == before
    assert links(db_conn, name_id) == {}
    assert _artists(db_conn) == artists
    assert _stubs_of(db_conn, VARIOUS_ARTISTS) == 0
    enqueue.assert_not_called()


# --- C75 -----------------------------------------------------------------------------------------


def _dependencies(dependant: Dependant) -> Iterator[Dependant]:
    for dependency in dependant.dependencies:
        yield dependency
        yield from _dependencies(dependency)


def _calls(dependant: Dependant) -> set[Callable[..., Any]]:
    return {d.call for d in _dependencies(dependant) if d.call is not None}


def test_both_routes_are_sync_routes_on_sync_repos() -> None:
    routes = {
        method: route
        for route in router.routes
        if isinstance(route, APIRoute) and route.path == "/artists/{artist_id}/not-an-artist"
        for method in route.methods
    }

    assert set(routes) == {"POST", "DELETE"}
    for method, route in routes.items():
        assert not inspect.iscoroutinefunction(route.endpoint), method
        assert get_sync_repos in _calls(route.dependant), method
        assert get_db_connection not in _calls(route.dependant), method
        # One connection, one transaction: the one cached, "function"-scoped SyncRepos.
        uses = [d for d in _dependencies(route.dependant) if d.call is get_sync_repos]
        assert len({d.cache_key for d in uses}) == 1, method
        assert all(d.scope == "function" and d.use_cache for d in uses), method

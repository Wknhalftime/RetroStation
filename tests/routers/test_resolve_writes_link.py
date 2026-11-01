"""Resolve and Unmatch write a log name's links in one transaction (artist model phase 1a, SPEC
S9, S10, E5; C75, C30, AUD-R035).

S9: ``POST /matching/artists/{id}/resolve`` is a sync route on ``SyncRepos``, so its status
write, the cascade to the name's songs and the manual link (written through the link service)
share one connection and one transaction. ``target_artist_id``, an MBID or an ``artists.id``
with no format check, resolves to the catalog artist with that id, else the one holding that
MBID, else a new stub of S3's shape. The link is manual, active, strength 100. The name's other
links stay. Resolving again to the same artist leaves one manual link (E5), and an automatic
link to that artist becomes the manual one. The enqueue stays in the route, after the commit
(the locked ``test_matching.py`` patches it there).

S10: ``POST /matching/artists/{id}/unmatch`` is a sync route the same way. It removes every
link of the name, as today's DELETE removed its artist rows (the locked
``test_matching_unmatch.py``; phase 1b turns them rejected instead, C19). A refused unmatch
(409) writes nothing.

Bodies: the fields of today's request and response stay, with their types and required flags;
a later phase may add fields (1b adds counts).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.dependencies import get_db_connection, get_sync_repos
from backend.domain.enums import LogNameLinkOrigin, LogNameLinkState, MatchStatus
from backend.routers.matching import router
from tests.matching_seed import seed_pg_link
from tests.routers.test_matching import (
    _insert_artist,
    _insert_event,
    _insert_identity,
    _insert_playlist,
    _insert_station,
)

Conn = psycopg.Connection[Any]

METALLICA = "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"
MEGADETH = "a9044915-8be3-4c7e-b11f-9e2d2ea0a91e"
UNKNOWN = "00000000-0000-4000-8000-0000000000aa"
ROUTES = {
    ("POST", "/artists/{artist_id}/resolve"),
    ("POST", "/artists/{artist_id}/unmatch"),
}


def _resolve(client: TestClient, name_id: UUID, target: str) -> httpx.Response:
    return client.post(
        f"/api/v1/matching/artists/{name_id}/resolve",
        json={"match_status": "manual_matched", "target_artist_id": target},
    )


def _musicbrainz(conn: Conn, name: str, mbid: str) -> str:
    artist_id = str(uuid4())
    conn.execute(
        "INSERT INTO artists (id, name, sort_name, normalized_name, mbid, origin,"
        " needs_enhancement) VALUES (%s, %s, %s, %s, %s, 'musicbrainz', FALSE)",
        (artist_id, name, name, name.lower(), mbid),
    )
    conn.commit()
    return artist_id


def _local(conn: Conn, name: str) -> str:
    artist_id = str(uuid4())
    conn.execute(
        "INSERT INTO artists (id, name, sort_name, normalized_name, origin, needs_enhancement)"
        " VALUES (%s, %s, %s, %s, 'local', FALSE)",
        (artist_id, name, name, name.lower()),
    )
    conn.commit()
    return artist_id


def _links(conn: Conn, name_id: UUID) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM log_name_links WHERE broadcast_artist_id = %s ORDER BY created_at",
        (name_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _artist_count(conn: Conn) -> int:
    row = conn.execute("SELECT count(*) AS n FROM artists").fetchone()
    assert row is not None
    return int(row["n"])


def _status(conn: Conn, table: str, row_id: UUID) -> str:
    row = conn.execute(f"SELECT match_status FROM {table} WHERE id = %s", (row_id,)).fetchone()
    assert row is not None
    return str(row["match_status"])


@pytest.fixture
def enqueue(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr("backend.routers.matching.identity_matching_task", mock)
    return mock


# --- C75: sync routes, bodies unchanged ----------------------------------------------------------


def _routes() -> dict[tuple[str, str], APIRoute]:
    return {
        (method, route.path): route
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if (method, route.path) in ROUTES
    }


def _dependencies(dependant: Dependant) -> Iterator[Dependant]:
    for dependency in dependant.dependencies:
        yield dependency
        yield from _dependencies(dependency)


def _calls(dependant: Dependant) -> set[Callable[..., Any]]:
    return {d.call for d in _dependencies(dependant) if d.call is not None}


def test_resolve_and_unmatch_are_sync_routes_on_one_connection() -> None:
    routes = _routes()

    assert set(routes) == ROUTES
    for key, route in routes.items():
        assert not inspect.iscoroutinefunction(route.endpoint), key
        calls = _calls(route.dependant)
        assert get_sync_repos in calls, key
        assert get_db_connection not in calls, key
        uses = [d for d in _dependencies(route.dependant) if d.call is get_sync_repos]
        assert len({d.cache_key for d in uses}) == 1, key
        assert all(d.scope == "function" and d.use_cache for d in uses), key


def test_the_bodies_do_not_change() -> None:
    """Today's fields stay, with their JSON types and required flags; a later phase may add
    fields of any shape (1b adds counts)."""
    app = FastAPI()
    app.include_router(router)
    spec = app.openapi()
    components = spec["components"]["schemas"]

    def json_type(field: dict[str, Any]) -> str:
        if "anyOf" in field:
            return "|".join(option.get("type", "ref") for option in field["anyOf"])
        return str(field.get("type", "ref"))

    def fields(ref: str, names: set[str]) -> dict[str, tuple[str, bool]]:
        model = components[ref.removeprefix("#/components/schemas/")]
        required = set(model.get("required", []))
        found = model["properties"]
        return {n: (json_type(found[n]), n in required) for n in names if n in found}

    resolve = spec["paths"]["/artists/{artist_id}/resolve"]["post"]
    unmatch = spec["paths"]["/artists/{artist_id}/unmatch"]["post"]
    request = resolve["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    todays_request = {
        "match_status": ("string", True),
        "target_artist_id": ("string|null", False),
    }
    todays_answer = {"id": ("string", True), "match_status": ("string", True)}

    assert fields(request, set(todays_request)) == todays_request
    assert "requestBody" not in unmatch
    for operation in (resolve, unmatch):
        answer = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        assert fields(answer, set(todays_answer)) == todays_answer


# --- S9: the manual link -------------------------------------------------------------------------


def test_unknown_mbid_gets_a_stub(client: TestClient, db_conn: Conn, enqueue: MagicMock) -> None:
    name = _insert_artist(db_conn, "RULE BAND")
    before = _artist_count(db_conn)

    response = _resolve(client, name.id, UNKNOWN)

    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["match_status"]) == (str(name.id), "manual_matched")
    [link] = _links(db_conn, name.id)
    stub = db_conn.execute("SELECT * FROM artists WHERE id = %s", (link["artist_id"],)).fetchone()
    assert stub is not None
    assert (stub["mbid"], stub["name"], stub["sort_name"], stub["normalized_name"]) == (
        UNKNOWN,
        UNKNOWN,
        UNKNOWN,
        None,
    )
    assert (stub["origin"], stub["needs_enhancement"]) == ("musicbrainz", True)
    UUID(stub["id"])
    assert stub["id"] != UNKNOWN
    assert (link["mbid"], link["state"], link["origin"], link["strength"]) == (
        UNKNOWN,
        "active",
        "manual",
        100,
    )
    assert link["evidence"]["version"] == 0
    assert _artist_count(db_conn) == before + 1
    assert _status(db_conn, "broadcast_artists", name.id) == "manual_matched"


def test_the_target_resolves_by_catalog_id_or_by_mbid(
    client: TestClient, db_conn: Conn, enqueue: MagicMock
) -> None:
    local = _local(db_conn, "Van Halen")
    metallica = _musicbrainz(db_conn, "Metallica", METALLICA)
    by_id = _insert_artist(db_conn, "VAN HALEN")
    by_mbid = _insert_artist(db_conn, "METALLICA")
    by_id_with_mbid = _insert_artist(db_conn, "METALLICA (LIVE)")
    before = _artist_count(db_conn)

    for name, target in ((by_id, local), (by_mbid, METALLICA), (by_id_with_mbid, metallica)):
        assert _resolve(client, name.id, target).status_code == 200

    assert [link["artist_id"] for link in _links(db_conn, by_id.id)] == [local]
    assert [(link["artist_id"], link["mbid"]) for link in _links(db_conn, by_mbid.id)] == [
        (metallica, METALLICA)
    ]
    assert [link["artist_id"] for link in _links(db_conn, by_id_with_mbid.id)] == [metallica]
    assert _artist_count(db_conn) == before


def test_other_links_stay(client: TestClient, db_conn: Conn, enqueue: MagicMock) -> None:
    metallica = _musicbrainz(db_conn, "Metallica", METALLICA)
    megadeth = _musicbrainz(db_conn, "Megadeth", MEGADETH)
    name = _insert_artist(db_conn, "METALLICA/MEGADETH", MatchStatus.AUTO_MATCHED)
    seed_pg_link(db_conn, name.id, METALLICA, origin=LogNameLinkOrigin.AUTOMATIC, strength=82)
    db_conn.commit()

    assert _resolve(client, name.id, MEGADETH).status_code == 200

    links = {link["artist_id"]: link for link in _links(db_conn, name.id)}
    assert set(links) == {metallica, megadeth}
    assert (links[metallica]["origin"], links[metallica]["strength"]) == ("automatic", 82)
    assert (links[megadeth]["origin"], links[megadeth]["state"]) == ("manual", "active")


def test_resolve_twice(client: TestClient, db_conn: Conn, enqueue: MagicMock) -> None:
    """E5: pressed twice, one manual link (and one stub)."""
    name = _insert_artist(db_conn, "RULE BAND")

    first = _resolve(client, name.id, UNKNOWN)
    after_first = _artist_count(db_conn)
    second = _resolve(client, name.id, UNKNOWN)

    assert (first.status_code, second.status_code) == (200, 200)
    [link] = _links(db_conn, name.id)
    assert (link["origin"], link["state"], link["strength"]) == ("manual", "active", 100)
    assert _artist_count(db_conn) == after_first


def test_resolving_to_an_automatically_linked_artist_makes_its_link_manual(
    client: TestClient, db_conn: Conn, enqueue: MagicMock
) -> None:
    metallica = _musicbrainz(db_conn, "Metallica", METALLICA)
    name = _insert_artist(db_conn, "METALICA", MatchStatus.AUTO_MATCHED)
    seed_pg_link(db_conn, name.id, metallica, origin=LogNameLinkOrigin.AUTOMATIC, strength=88)
    db_conn.commit()

    assert _resolve(client, name.id, metallica).status_code == 200

    [link] = _links(db_conn, name.id)
    assert (link["artist_id"], link["origin"], link["state"], link["strength"]) == (
        metallica,
        "manual",
        "active",
        100,
    )


@pytest.mark.parametrize(
    ("table", "operation"),
    [("log_name_links", "INSERT OR UPDATE"), ("track_identities", "UPDATE")],
    ids=["the_link_write_fails", "the_cascade_fails_after_the_link"],
)
def test_one_transaction(
    client: TestClient, db_conn: Conn, enqueue: MagicMock, table: str, operation: str
) -> None:
    """A write the route makes fails at the database: the link write itself (after the status
    write and the stub), or the cascade to the songs (after the link): none of them is left,
    and nothing is enqueued."""
    station = _insert_station(db_conn)
    playlist = _insert_playlist(db_conn, station)
    name = _insert_artist(db_conn, "RULE BAND", MatchStatus.NEEDS_REVIEW)
    song = _insert_identity(db_conn, name, "Some Song", MatchStatus.NEEDS_REVIEW)
    _insert_event(db_conn, playlist, song)
    before = _artist_count(db_conn)
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
        response = _resolve(client, name.id, UNKNOWN)
    finally:
        db_conn.execute(f"DROP TRIGGER IF EXISTS test_refuse_write ON {table}")
        db_conn.execute("DROP FUNCTION IF EXISTS test_refuse_write()")
        db_conn.commit()

    assert response.status_code == 500
    assert _status(db_conn, "broadcast_artists", name.id) == "needs_review"
    assert _status(db_conn, "track_identities", song.id) == "needs_review"
    assert _links(db_conn, name.id) == []
    assert _artist_count(db_conn) == before
    enqueue.assert_not_called()


# --- S10: unmatch --------------------------------------------------------------------------------


def test_unmatch_is_one_transaction(client: TestClient, db_conn: Conn, enqueue: MagicMock) -> None:
    """The link write fails at the database, after the name's and its songs' status writes:
    none of them is left."""
    name = _insert_artist(db_conn, "METALLICA", MatchStatus.MANUAL_MATCHED)
    song = _insert_identity(db_conn, name, "One", MatchStatus.MANUAL_MATCHED)
    seed_pg_link(db_conn, name.id, METALLICA, origin=LogNameLinkOrigin.MANUAL)
    db_conn.execute(
        """CREATE OR REPLACE FUNCTION test_refuse_write() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN RAISE EXCEPTION 'write refused'; END $$"""
    )
    db_conn.execute(
        "CREATE TRIGGER test_refuse_write BEFORE UPDATE OR DELETE ON log_name_links"
        " FOR EACH ROW EXECUTE FUNCTION test_refuse_write()"
    )
    db_conn.commit()
    try:
        response = client.post(f"/api/v1/matching/artists/{name.id}/unmatch")
    finally:
        db_conn.execute("DROP TRIGGER IF EXISTS test_refuse_write ON log_name_links")
        db_conn.execute("DROP FUNCTION IF EXISTS test_refuse_write()")
        db_conn.commit()

    assert response.status_code == 500
    assert _status(db_conn, "broadcast_artists", name.id) == "manual_matched"
    assert _status(db_conn, "track_identities", song.id) == "manual_matched"
    assert [(link["state"], link["origin"]) for link in _links(db_conn, name.id)] == [
        ("active", "manual")
    ]


@pytest.mark.parametrize("status", [MatchStatus.PENDING, MatchStatus.NEEDS_REVIEW])
def test_a_refused_unmatch_keeps_the_links(
    client: TestClient, db_conn: Conn, enqueue: MagicMock, status: MatchStatus
) -> None:
    name = _insert_artist(db_conn, "METALLICA", status)
    seed_pg_link(db_conn, name.id, METALLICA, state=LogNameLinkState.SUGGESTED)
    db_conn.commit()

    response = client.post(f"/api/v1/matching/artists/{name.id}/unmatch")

    assert response.status_code == 409
    assert len(_links(db_conn, name.id)) == 1

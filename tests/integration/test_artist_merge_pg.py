"""Merge and Unmerge on the artist page, phase 2a PR 3 (SPEC S11, E4, E5; M33, C75; AUD-R041), on
PostgreSQL through the routes.

S11 (M33): every artist is its own, and nothing merges two automatically. ``artist_merges``
(migration 0049) holds the direct edge: the artist merged away, its primary, ``merged_by`` (the
constant ``curator``), when, and a reason. ``POST /api/v1/library/artists/{id}/merge`` with a
``primary_id`` merges ``{id}`` away; ``DELETE`` on the same path deletes its one edge. Both answer
204.

- The merged artist leaves the artist list, its count and its search. Its detail names the
  group's primary in ``merged_into``, and the page redirects there. The primary's detail lists
  its ``members`` and shows their works; with no works, the files of every member MBID. The list
  counts the group's works and files on the primary.
- ``GET /api/v1/library/artists?mbid=`` finds the artist holding that MBID, and a merged member's
  MBID finds its primary (M33: lookups by either MBID land there).
- ``artist_group(id)`` (``PgArtistMergeRepository``) walks to the root and returns an
  ``ArtistGroup``: the primary id, the member ids (the primary among them) and the member MBIDs
  (a name-only member has none). An unknown id has none.
- Unmerge restores everything: the list and both details are as they were before the merge.
- Merge and Unmerge are idempotent. A merge of an artist already merged into another is refused.
  A merge into an artist that is itself merged is kept as made, and the group walks past it.
- Credits and MusicBrainz lookups keep the real MBID: ``get_by_mbid`` answers with the merged
  artist itself, the files keep their tags, and each log-name link stays on the artist it names.
  A merged artist stays in the matcher's pool (``list_all``). Song matching doesn't read the
  group until 4.3, so neither action queues a task or moves a log name's status.
- ``_delete_unused_local_artists`` keeps an artist on either side of an edge.
- Both write routes are sync routes on ``SyncRepos`` (C75), and a lost connection answers 503.
- Nothing merges automatically: only the adapter writes the table, only the merge service calls
  its writes, and only the artists router imports that service.

E4: A merged into B, then B into C. Each edge stays as made and ``artist_group`` walks to C.
Unmerging one edge restores only that edge.

E5: a merge of an artist into itself, one that would make a cycle, or one with a special-purpose
artist (``SPECIAL_PURPOSE_MBIDS``) on either side answers 409; an unknown artist on either side
answers 404. Nothing is written.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from huey.storage import MemoryStorage
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

import backend.db.sync_conn as sync_conn
from backend.config import get_settings
from backend.db.repositories.artist_merges import PgArtistMergeRepository
from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.works import PgWorkRepository
from backend.dependencies import get_current_token, get_db_connection, get_sync_repos
from backend.domain.catalog import SPECIAL_PURPOSE_MBIDS, ArtistGroup
from backend.domain.enums import MatchStatus
from backend.repositories.artist_merges import ArtistMergeRepository
from backend.routers.library.artists import router as artists_router
from backend.routers.v1 import router as v1_router
from backend.services.normalization import normalize_artist
from backend.tasks.huey_app import huey
from tests.lifespan_client import lifespan_client
from tests.routers.cascade_seed import CascadeWorld

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]

# psycopg's async pool needs the selector event loop on Windows (as tests/routers/conftest.py).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

ROOT = Path(__file__).resolve().parents[2]
ARTISTS = "/api/v1/library/artists"

BAND = "4d7928cd-7ed2-4282-8c29-c0c9f966f1bd"  # Alice Cooper, the band
SINGER = "ee58c59f-8e7f-4430-b8ca-236c4d3745ae"  # Alice Cooper, the singer
PRINCE = "070d193a-845c-479f-980e-bef15710653e"
NPG = "0a000000-0000-4000-8000-0000000000d1"  # Prince & The New Power Generation
NOBODY = "0a000000-0000-4000-8000-0000000000d2"  # an MBID no artist holds


@pytest.fixture(scope="module", autouse=True)
def queue_in_memory() -> Iterator[None]:
    """Anything a route queues lands in memory, never in a worker's queue file."""
    patch = pytest.MonkeyPatch()
    patch.setattr(huey, "storage", MemoryStorage(huey.name))
    yield
    patch.undo()


@pytest.fixture(scope="module")
def app_client(_migrated_db_url: str) -> Iterator[TestClient]:
    """The app on the test database, its lifespan run once per module; the token is bypassed."""
    patch = pytest.MonkeyPatch()
    patch.setenv("DATABASE_URL", _migrated_db_url)
    patch.setenv("RETROSTATION_SKIP_BOOT_MIGRATIONS", "1")
    get_settings.cache_clear()
    from backend.main import app

    async def _token() -> str:
        return "test-token"

    app.dependency_overrides[get_current_token] = _token
    try:
        with lifespan_client(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        patch.undo()


@pytest.fixture
def client(app_client: TestClient, migrated_db: str) -> TestClient:
    """The module's client; tables are truncated before each test."""
    huey.flush()
    return app_client


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


# --- seeds ---------------------------------------------------------------------------------------


def _artist(conn: Conn, name: str, mbid: str | None = None) -> str:
    """A named artist: name-only (``local``) without an MBID, else a MusicBrainz artist."""
    row = conn.execute(
        """INSERT INTO artists
               (id, name, sort_name, normalized_name, mbid, origin, needs_enhancement)
           VALUES (gen_random_uuid()::text, %(name)s, %(name)s, %(norm)s, %(mbid)s, %(origin)s,
                   FALSE)
           RETURNING id""",
        {
            "name": name,
            "norm": normalize_artist(name),
            "mbid": mbid,
            "origin": "musicbrainz" if mbid else "local",
        },
    ).fetchone()
    assert row is not None
    conn.commit()
    return str(row["id"])


def _work(conn: Conn, artist_id: str, title: str) -> str:
    work_id = PgWorkRepository(conn).create_local(title, artist_id)
    conn.commit()
    return work_id


def _file(conn: Conn, title: str, *, mbid: str | None = None, work_id: str | None = None) -> str:
    """A present file, tagged with *mbid* when given, on *work_id* when given."""
    row = conn.execute(
        """INSERT INTO library_files
               (file_path, format, track_title, artist_name, artist_mbid, artist_mbids, credit,
                work_id, file_status)
           VALUES (%s, 'flac', %s, 'Alice Cooper', %s, %s, %s, %s, 'present')
           RETURNING id""",
        (
            f"/music/{uuid4().hex}.flac",
            title,
            mbid,
            [mbid] if mbid else [],
            None if mbid is None else Jsonb([{"name": "Alice Cooper", "mbid": mbid, "join": None}]),
            work_id,
        ),
    ).fetchone()
    assert row is not None
    conn.commit()
    return str(row["id"])


# --- the routes and reads ------------------------------------------------------------------------


def _merge(
    client: TestClient, artist_id: str, primary_id: str, reason: str | None = None
) -> httpx.Response:
    body: dict[str, str] = {"primary_id": primary_id}
    if reason is not None:
        body["reason"] = reason
    return client.post(f"{ARTISTS}/{artist_id}/merge", json=body)


def _unmerge(client: TestClient, artist_id: str) -> httpx.Response:
    return client.delete(f"{ARTISTS}/{artist_id}/merge")


def _listed(client: TestClient, **params: str) -> tuple[int, dict[str, dict[str, Any]]]:
    """The artist list: its total and its items by id."""
    response = client.get(ARTISTS, params={"limit": "500", **params})
    assert response.status_code == 200
    body = response.json()
    return body["total"], {item["id"]: item for item in body["items"]}


def _ids(client: TestClient, **params: str) -> tuple[int, set[str]]:
    total, items = _listed(client, **params)
    return total, set(items)


def _detail(client: TestClient, artist_id: str) -> dict[str, Any]:
    response = client.get(f"{ARTISTS}/{artist_id}")
    assert response.status_code == 200
    return dict(response.json())


def _edges(conn: Conn) -> dict[str, str]:
    """Each merged artist's direct primary."""
    conn.commit()
    rows = conn.execute("SELECT artist_id, primary_id FROM artist_merges").fetchall()
    return {r["artist_id"]: r["primary_id"] for r in rows}


def _rows(conn: Conn) -> list[dict[str, Any]]:
    conn.commit()
    rows = conn.execute("SELECT * FROM artist_merges ORDER BY artist_id").fetchall()
    return [dict(r) for r in rows]


def _group(conn: Conn, artist_id: str) -> ArtistGroup | None:
    conn.commit()
    return PgArtistMergeRepository(conn).artist_group(artist_id)


def _group_of(primary: str, members: set[str], mbids: set[str]) -> ArtistGroup:
    return ArtistGroup(
        primary_id=primary, member_ids=frozenset(members), member_mbids=frozenset(mbids)
    )


def _members(detail: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    return [(m["id"], m["name"], m["mbid"]) for m in detail["members"]]


def _exists(conn: Conn, artist_id: str) -> bool:
    conn.commit()
    return conn.execute("SELECT 1 FROM artists WHERE id = %s", (artist_id,)).fetchone() is not None


# --- S11: Merge ----------------------------------------------------------------------------------


def test_merge_hides_the_artist_and_its_works_show_under_the_primary(
    client: TestClient, conn: Conn
) -> None:
    local = _artist(conn, "Alice Cooper")
    band = _artist(conn, "Alice Cooper", BAND)
    other = _artist(conn, "Ozzy Osbourne")
    schools_out = _work(conn, local, "School's Out")
    poison = _work(conn, band, "Poison")
    _file(conn, "School's Out", work_id=schools_out)
    _file(conn, "School's Out", work_id=schools_out)
    _file(conn, "Poison", mbid=BAND, work_id=poison)

    assert _merge(client, local, band, reason="one act").status_code == 204

    total, items = _listed(client)
    assert (total, set(items)) == (2, {band, other})
    assert (items[band]["work_count"], items[band]["file_count"]) == (2, 3)
    assert _ids(client, search="alice") == (1, {band})

    assert _detail(client, local)["merged_into"] == band
    primary = _detail(client, band)
    assert primary["merged_into"] is None
    assert _members(primary) == [(local, "Alice Cooper", None)]
    assert {w["id"] for w in primary["works"]} == {schools_out, poison}
    alone = _detail(client, other)
    assert (alone["merged_into"], alone["members"]) == (None, [])

    [row] = _rows(conn)
    assert (row["artist_id"], row["primary_id"], row["merged_by"], row["reason"]) == (
        local,
        band,
        "curator",
        "one act",
    )
    assert row["merged_at"] is not None


def test_files_show_under_the_primary(client: TestClient, conn: Conn) -> None:
    """Artists with no works (such as those S14's sweep makes) show their files by MBID: the
    primary shows the files of every member MBID."""
    prince = _artist(conn, "Prince", PRINCE)
    npg = _artist(conn, "Prince & The New Power Generation", NPG)
    _file(conn, "Purple Rain", mbid=PRINCE)
    _file(conn, "Cream", mbid=NPG)
    _file(conn, "Diamonds and Pearls", mbid=NPG)

    assert _merge(client, npg, prince).status_code == 204

    titles = {w["title"] for w in _detail(client, prince)["works"]}
    assert titles == {"Purple Rain", "Cream", "Diamonds and Pearls"}


def test_unmerge_restores_everything(client: TestClient, conn: Conn) -> None:
    local = _artist(conn, "Alice Cooper")
    band = _artist(conn, "Alice Cooper", BAND)
    _artist(conn, "Ozzy Osbourne")
    schools_out = _work(conn, local, "School's Out")
    _file(conn, "School's Out", work_id=schools_out)
    _file(conn, "Poison", mbid=BAND, work_id=_work(conn, band, "Poison"))
    before = (_listed(client), _detail(client, local), _detail(client, band))

    assert _merge(client, local, band).status_code == 204
    assert _unmerge(client, local).status_code == 204

    assert (_listed(client), _detail(client, local), _detail(client, band)) == before
    assert _edges(conn) == {}


def test_merge_and_unmerge_are_idempotent(client: TestClient, conn: Conn) -> None:
    singer = _artist(conn, "Alice Cooper", SINGER)
    band = _artist(conn, "Alice Cooper", BAND)

    assert _merge(client, singer, band, reason="first").status_code == 204
    first = _rows(conn)
    assert _merge(client, singer, band, reason="again").status_code == 204
    assert _rows(conn) == first

    assert _unmerge(client, singer).status_code == 204
    assert _unmerge(client, singer).status_code == 204
    assert _unmerge(client, band).status_code == 204  # never merged
    assert _edges(conn) == {}


def test_a_merged_artist_is_not_merged_elsewhere(client: TestClient, conn: Conn) -> None:
    singer = _artist(conn, "Alice Cooper", SINGER)
    band = _artist(conn, "Alice Cooper", BAND)
    prince = _artist(conn, "Prince", PRINCE)
    assert _merge(client, singer, band).status_code == 204

    assert _merge(client, singer, prince).status_code == 409

    assert _edges(conn) == {singer: band}


def test_a_merge_into_a_merged_artist_is_kept_as_made(client: TestClient, conn: Conn) -> None:
    singer = _artist(conn, "Alice Cooper", SINGER)
    band = _artist(conn, "Alice Cooper", BAND)
    local = _artist(conn, "Alice Cooper")
    assert _merge(client, singer, band).status_code == 204

    assert _merge(client, local, singer).status_code == 204

    assert _edges(conn) == {singer: band, local: singer}
    assert _group(conn, local) == _group_of(band, {band, singer, local}, {BAND, SINGER})
    assert _detail(client, local)["merged_into"] == band


@pytest.mark.parametrize("missing", ["merged", "primary"])
def test_an_unknown_artist_is_404(client: TestClient, conn: Conn, missing: str) -> None:
    band = _artist(conn, "Alice Cooper", BAND)
    unknown = str(uuid4())
    pair = (unknown, band) if missing == "merged" else (band, unknown)

    assert _merge(client, *pair).status_code == 404
    if missing == "merged":
        assert _unmerge(client, unknown).status_code == 404

    assert _edges(conn) == {}


def test_the_mbid_filter_finds_the_primary(client: TestClient, conn: Conn) -> None:
    band = _artist(conn, "Alice Cooper", BAND)
    singer = _artist(conn, "Alice Cooper", SINGER)
    _artist(conn, "Ozzy Osbourne")

    assert _ids(client, mbid=BAND) == (1, {band})
    assert _ids(client, mbid=SINGER) == (1, {singer})
    assert _ids(client, mbid=NOBODY) == (0, set())

    assert _merge(client, singer, band).status_code == 204

    assert _ids(client, mbid=SINGER) == (1, {band})
    assert _ids(client, mbid=BAND) == (1, {band})


def test_links_credits_and_lookups_keep_the_real_artist(client: TestClient, conn: Conn) -> None:
    world = CascadeWorld(conn)
    singer = _artist(conn, "Alice Cooper", SINGER)
    band = _artist(conn, "Alice Cooper", BAND)
    name_id = world.name("ALICE COOPER", MatchStatus.AUTO_MATCHED)
    world.link(name_id, singer)
    _file(conn, "Poison", mbid=SINGER, work_id=_work(conn, singer, "Poison"))

    def kept() -> tuple[list[dict[str, Any]], ...]:
        conn.commit()
        return tuple(
            [dict(r) for r in conn.execute(sql).fetchall()]
            for sql in (
                "SELECT * FROM log_name_links ORDER BY id",
                "SELECT id, artist_mbid, artist_mbids, credit, work_id FROM library_files",
                "SELECT id, match_status, reason_code FROM broadcast_artists",
                "SELECT id, artist_id FROM works ORDER BY id",
            )
        )

    before = kept()
    for action in (lambda: _merge(client, singer, band), lambda: _unmerge(client, singer)):
        assert action().status_code == 204

        assert kept() == before
        catalog = PgArtistRepository(conn)
        found = catalog.get_by_mbid(SINGER)
        assert found is not None and found.id == singer
        assert singer in {artist.id for artist in catalog.list_all()}
        assert huey.pending_count() == 0


# --- E4 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("unmerged", ["first edge", "second edge"])
def test_chain_walks_to_the_primary(client: TestClient, conn: Conn, unmerged: str) -> None:
    first = _artist(conn, "Alice Cooper")  # name-only: no MBID
    second = _artist(conn, "Alice Cooper", SINGER)
    third = _artist(conn, "Alice Cooper", BAND)
    works = {_work(conn, artist, f"Song {n}") for n, artist in enumerate((first, second, third))}
    assert _merge(client, first, second).status_code == 204
    assert _merge(client, second, third).status_code == 204

    assert _edges(conn) == {first: second, second: third}
    whole = _group_of(third, {first, second, third}, {SINGER, BAND})
    assert [_group(conn, a) for a in (first, second, third)] == [whole, whole, whole]
    assert _detail(client, first)["merged_into"] == third
    assert _detail(client, second)["merged_into"] == third
    primary = _detail(client, third)
    assert {m["id"] for m in primary["members"]} == {first, second}
    assert {w["id"] for w in primary["works"]} == works
    assert _ids(client)[1] == {third}
    assert _group(conn, str(uuid4())) is None

    if unmerged == "first edge":
        assert _unmerge(client, first).status_code == 204
        assert _edges(conn) == {second: third}
        assert _group(conn, first) == _group_of(first, {first}, set())
        rest = _group_of(third, {second, third}, {SINGER, BAND})
        assert (_group(conn, second), _group(conn, third)) == (rest, rest)
        assert _ids(client)[1] == {first, third}
    else:
        assert _unmerge(client, second).status_code == 204
        assert _edges(conn) == {first: second}
        pair = _group_of(second, {first, second}, {SINGER})
        assert (_group(conn, first), _group(conn, second)) == (pair, pair)
        assert _group(conn, third) == _group_of(third, {third}, {BAND})
        assert _detail(client, first)["merged_into"] == second
        assert _ids(client)[1] == {second, third}


# --- E5 ------------------------------------------------------------------------------------------


def test_no_cycles(client: TestClient, conn: Conn) -> None:
    first = _artist(conn, "Alice Cooper")
    second = _artist(conn, "Alice Cooper", SINGER)
    third = _artist(conn, "Alice Cooper", BAND)

    assert _merge(client, first, first).status_code == 409  # itself
    assert _merge(client, first, second).status_code == 204
    assert _merge(client, second, first).status_code == 409  # a cycle of two
    assert _merge(client, second, third).status_code == 204
    assert _merge(client, third, first).status_code == 409  # a cycle of three

    assert _edges(conn) == {first: second, second: third}


@pytest.mark.parametrize("side", ["merged", "primary"])
@pytest.mark.parametrize("mbid", sorted(SPECIAL_PURPOSE_MBIDS))
def test_placeholder_refused(client: TestClient, conn: Conn, mbid: str, side: str) -> None:
    placeholder = _artist(conn, "Placeholder", mbid)
    real = _artist(conn, "Alice Cooper", BAND)
    pair = (placeholder, real) if side == "merged" else (real, placeholder)

    assert _merge(client, *pair).status_code == 409

    assert _edges(conn) == {}


# --- the table -----------------------------------------------------------------------------------


def test_the_table_refuses_a_bad_edge(conn: Conn) -> None:
    """0049's constraints, under the routes' own checks."""
    singer = _artist(conn, "Alice Cooper", SINGER)
    band = _artist(conn, "Alice Cooper", BAND)
    insert = "INSERT INTO artist_merges (artist_id, primary_id, merged_by) VALUES (%s, %s, %s)"

    for values, refused in (
        ((singer, singer, "curator"), psycopg.errors.CheckViolation),
        ((singer, band, "matcher"), psycopg.errors.CheckViolation),
        ((singer, str(uuid4()), "curator"), psycopg.errors.ForeignKeyViolation),
    ):
        with pytest.raises(refused):
            conn.execute(insert, values)
        conn.rollback()

    conn.execute(insert, (singer, band, "curator"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(insert, (singer, band, "curator"))
    conn.rollback()


# --- _delete_unused_local_artists ----------------------------------------------------------------


@pytest.mark.parametrize("side", ["merged", "primary", "neither"])
def test_an_artist_on_either_side_of_a_merge_is_kept(
    client: TestClient, conn: Conn, side: str
) -> None:
    """A work merge deletes a local artist left with no work and no link (AUD-R035), but not
    one on either side of an edge. ``neither`` shows the delete is reached."""
    local = _artist(conn, "Local Act")
    partner = _artist(conn, "Partner Act", BAND)
    leaving = _work(conn, local, "Only Song")
    staying = _work(conn, _artist(conn, "Target Act", SINGER), "Only Song")
    _file(conn, "Only Song", work_id=leaving)
    _file(conn, "Only Song", work_id=staying)
    if side == "merged":
        assert _merge(client, local, partner).status_code == 204
    elif side == "primary":
        assert _merge(client, partner, local).status_code == 204
    edges = _edges(conn)

    response = client.post(
        f"/api/v1/library/works/{staying}/merge", json={"source_work_ids": [leaving]}
    )

    assert response.status_code == 200
    assert _exists(conn, local) is (side != "neither")
    assert _edges(conn) == edges


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
        for route in artists_router.routes
        if isinstance(route, APIRoute) and route.path == "/artists/{artist_id}/merge"
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


@pytest.fixture
def bare_app(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    get_settings.cache_clear()
    built = FastAPI()
    built.include_router(v1_router)
    built.dependency_overrides[get_current_token] = lambda: "test-token"
    try:
        yield built
    finally:
        get_settings.cache_clear()


@pytest.fixture
def connections_die(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every sync connection has its backend terminated as soon as it opens; returns the pids."""
    real: Callable[..., psycopg.Connection[Any]] = sync_conn.connect_sync
    killed: list[int] = []

    def connect(dsn: str, **kwargs: Any) -> psycopg.Connection[Any]:
        opened = real(dsn, **kwargs)
        with psycopg.connect(migrated_db, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (opened.info.backend_pid,))
        killed.append(opened.info.backend_pid)
        return opened

    monkeypatch.setattr(sync_conn, "connect_sync", connect)
    return killed


@pytest.mark.parametrize("method", ["POST", "DELETE"])
async def test_a_lost_connection_is_503(
    bare_app: FastAPI, connections_die: list[int], method: str
) -> None:
    body = {"primary_id": str(uuid4())} if method == "POST" else None
    transport = httpx.ASGITransport(app=bare_app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.request(method, f"{ARTISTS}/{uuid4()}/merge", json=body)

    assert len(connections_die) == 1
    assert response.status_code == 503
    assert response.json() == {"detail": "unavailable"}


# --- nothing merges automatically ----------------------------------------------------------------

ADAPTER = "backend/db/repositories/artist_merges.py"
SERVICE = "backend/services/artist_merge_service.py"
SERVICE_MODULE = "backend.services.artist_merge_service"
ROUTER = "backend/routers/library/artists.py"
WRITES = {"add_merge", "remove_merge"}
SCANNED = ("backend", "scripts")
_WRITES_THE_TABLE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO|TRUNCATE(?:\s+TABLE)?|COPY)"
    r"\s+(?:ONLY\s+)?(?:public\.)?\"?artist_merges\b",
    re.IGNORECASE,
)


def _sources() -> Iterator[tuple[str, str]]:
    """Every .py and .sql file of backend/ and scripts/, but the migrations and rollbacks."""
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*")):
            if path.suffix not in {".py", ".sql"} or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(ROOT).as_posix()
            if relative.startswith("backend/db/migrations/") or re.fullmatch(
                r"backend/db/rollback_[^/]+\.sql", relative
            ):
                continue
            yield relative, path.read_text(encoding="utf-8")


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return found


def test_nothing_merges_automatically() -> None:
    assert ArtistMergeRepository.__abstractmethods__ >= WRITES

    writers: set[str] = set()
    callers: set[str] = set()
    importers: set[str] = set()
    for relative, source in _sources():
        if _WRITES_THE_TABLE.search(source):
            writers.add(relative)
        if not relative.endswith(".py"):
            continue
        tree = ast.parse(source)
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in WRITES
            for node in ast.walk(tree)
        ):
            callers.add(relative)
        if SERVICE_MODULE in _imports(tree):
            importers.add(relative)

    assert writers == {ADAPTER}
    assert callers == {SERVICE}
    assert importers == {ROUTER}

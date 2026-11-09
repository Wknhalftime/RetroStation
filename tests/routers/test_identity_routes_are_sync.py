"""A song's Resolve, Reject and Unmatch are sync routes on SyncRepos (artist model phase 3a
PR 1, SPEC S4; C75, C52).

``POST /identities/{identity_id}/resolve`` (Resolve, and Reject with ``manual_rejected``) and
``POST /identities/{identity_id}/unmatch`` are plain ``def`` routes, which FastAPI runs in a
worker thread, on the request's one sync connection from ``get_sync_repos``, through
``SyncRepos``'s single "function"-scoped, cached dependency: the song's status and its matches,
written through ``SongMatchService``, commit together, and a route that raises rolls back all
of it. Neither takes the async ``get_db_connection`` or runs SQL of its own (no ``.execute``,
nothing named from psycopg or ``backend.db``; reached is followed through the module's own
functions, classes and assignments, as in ``tests/test_works_router_writes_have_no_sql.py``).
Their status codes and bodies are today's (field names, JSON types, required fields).

The locked ``test_matching.py`` and ``test_matching_unmatch.py`` keep pinning what the routes
do. These cases pin what moving them onto ``SyncRepos`` could lose:

- one sync connection per request, and the manual match committed before the master
  recalculation, which runs on its own connection (``recalculate_for_work_sync`` keeps its name
  and signature in ``routers/matching.py``; a file with no work skips it);
- a match write the database refuses rolls the song's status back with it;
- the song's row lock comes first (``FOR UPDATE``): Reject and Unmatch wait for a worker's
  uncommitted match and decide on what it committed, and Resolve's manual match replaces it;
- a lost connection answers ``503 unavailable``, as the artist routes do;
- ``identity_resolution_service.record_song_rejection`` (named by AUD-R029) and
  ``persist_manual_match`` keep their names, as sync functions.
"""

from __future__ import annotations

import ast
import inspect
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from backend.db import sync_conn
from backend.dependencies import get_db_connection, get_sync_repos
from backend.domain.enums import MatchStatus, MatchTier
from backend.routers.matching import router
from backend.services import identity_resolution_service
from backend.services.repository_factory import RepositoryFactory
from tests.integration.missing_file_seed import Conn, identity, library_file, match, work

MATCHING_ROUTER = Path(__file__).resolve().parents[2] / "backend" / "routers" / "matching.py"
API = "/api/v1/matching"
RESOLVE = ("POST", "/identities/{identity_id}/resolve")
UNMATCH = ("POST", "/identities/{identity_id}/unmatch")
DB_MODULES = ("psycopg", "psycopg_pool", "backend.db")
SQL_CALLS = {"execute", "executemany"}

# A body as {field: JSON type}; a field the body may leave out ends in "?".
type Body = dict[str, str] | None

# (method, path) -> (success status, request body, response body), as today.
TODAYS_ROUTES: dict[tuple[str, str], tuple[int, Body, Body]] = {
    RESOLVE: (
        200,
        {"match_status": "string", "library_file_id?": "string|null"},
        {"id": "string", "match_status": "string"},
    ),
    UNMATCH: (200, None, {"id": "string", "match_status": "string"}),
}


def _routes() -> dict[tuple[str, str], APIRoute]:
    return {
        (method, route.path): route
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if (method, route.path) in TODAYS_ROUTES
    }


def _dependencies(dependant: Dependant) -> Iterator[Dependant]:
    for dependency in dependant.dependencies:
        yield dependency
        yield from _dependencies(dependency)


def _calls(dependant: Dependant) -> set[Callable[..., Any]]:
    return {d.call for d in _dependencies(dependant) if d.call is not None}


# --- the routes, statically --------------------------------------------------------------------


def test_the_identity_routes_are_there() -> None:
    assert sorted(set(TODAYS_ROUTES) - set(_routes())) == []


def test_they_are_sync_routes_on_sync_repos() -> None:
    routes = _routes()
    is_async = inspect.iscoroutinefunction
    assert sorted(key for key, route in routes.items() if is_async(route.endpoint)) == []
    calls = {key: _calls(route.dependant) for key, route in routes.items()}
    assert sorted(key for key, found in calls.items() if get_sync_repos not in found) == []
    assert sorted(key for key, found in calls.items() if get_db_connection in found) == []


def test_each_has_one_connection_and_one_transaction() -> None:
    """Every use of get_sync_repos in a route is the one cached, "function"-scoped SyncRepos:
    a second key (another scope, or no cache) would open a second connection."""
    for key, route in _routes().items():
        uses = [d for d in _dependencies(route.dependant) if d.call is get_sync_repos]
        assert len({d.cache_key for d in uses}) == 1, key
        assert all(d.scope == "function" and d.use_cache for d in uses), key


def _body(schema: dict[str, Any] | None, components: dict[str, Any]) -> Body:
    if schema is None:
        return None
    model = components[schema["$ref"].removeprefix("#/components/schemas/")]
    required = set(model.get("required", []))

    def json_type(field: dict[str, Any]) -> str:
        if "anyOf" in field:
            return "|".join(str(option["type"]) for option in field["anyOf"])
        return str(field["type"])

    return {
        name if name in required else f"{name}?": json_type(field)
        for name, field in model["properties"].items()
    }


def test_their_bodies_and_status_codes_are_unchanged() -> None:
    app = FastAPI()
    app.include_router(router)
    spec = app.openapi()
    components = spec["components"]["schemas"]

    found: dict[tuple[str, str], tuple[int, Body, Body]] = {}
    for method, path in TODAYS_ROUTES:
        operation = spec["paths"][path][method.lower()]
        success = [int(code) for code in operation["responses"] if code.startswith("2")]
        assert len(success) == 1, (method, path)
        response = operation["responses"][str(success[0])]
        body = operation.get("requestBody", {}).get("content", {}).get("application/json")
        returned = response.get("content", {}).get("application/json")
        found[method, path] = (
            success[0],
            _body(body["schema"] if body else None, components),
            _body(returned["schema"] if returned else None, components),
        )

    assert found == TODAYS_ROUTES


def _module_names(tree: ast.Module) -> tuple[dict[str, ast.AST], dict[str, str]]:
    """The module's own definitions by name, and each imported name's module."""
    defined: dict[str, ast.AST] = {}
    imported: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            defined[node.name] = node
        elif isinstance(node, ast.Assign):
            defined |= {t.id: node for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined[node.target.id] = node
        elif isinstance(node, ast.TypeAlias):
            defined[node.name.id] = node
        elif isinstance(node, ast.ImportFrom):
            imported |= {a.asname or a.name: node.module or "" for a in node.names}
        elif isinstance(node, ast.Import):
            imported |= {a.asname or a.name.split(".")[0]: a.name for a in node.names}
    return defined, imported


def _is_db(module: str) -> bool:
    return any(module == m or module.startswith(f"{m}.") for m in DB_MODULES)


def _reached(
    name: str,
    node: ast.AST,
    module: tuple[dict[str, ast.AST], dict[str, str]],
    seen: set[str],
) -> set[str]:
    """What *node* (the module's *name*) reaches: ``name:line`` of each SQL call, and
    ``module.name`` of each name imported from a database module."""
    defined, imported = module
    found: set[str] = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in SQL_CALLS
        ):
            found.add(f"{name}:{child.lineno}")
        elif isinstance(child, ast.Name) and child.id in imported:
            if _is_db(imported[child.id]):
                found.add(f"{imported[child.id]}.{child.id}")
        elif isinstance(child, ast.Name) and child.id in defined and child.id not in seen:
            seen.add(child.id)
            found |= _reached(child.id, defined[child.id], module, seen)
    return found


def test_they_run_no_sql_of_their_own() -> None:
    tree = ast.parse(MATCHING_ROUTER.read_text(encoding="utf-8"))
    module = _module_names(tree)
    endpoints = {route.endpoint.__name__ for route in _routes().values()}

    reached = {name: sorted(_reached(name, module[0][name], module, {name})) for name in endpoints}

    assert reached == {name: [] for name in endpoints}


def test_the_song_writes_keep_their_names_as_sync_functions() -> None:
    for name in ("record_song_rejection", "persist_manual_match"):
        function = getattr(identity_resolution_service, name)
        assert inspect.isfunction(function), name
        assert not inspect.iscoroutinefunction(function), name


# --- the routes, on PostgreSQL -----------------------------------------------------------------


def _song(conn: Conn, status: MatchStatus) -> UUID:
    song = identity(RepositoryFactory(conn), status)
    conn.commit()
    return song


def _file(conn: Conn, name: str, work_id: str | None = None) -> UUID:
    row = library_file(RepositoryFactory(conn), rf"D:\music\{name}.flac", work_id, missing=False)
    conn.commit()
    return row.id


def _matched(conn: Conn, song: UUID, file_id: UUID) -> None:
    match(conn, song, file_id)
    conn.commit()


def _state(conn: Conn, song: UUID) -> tuple[str, str | None, list[UUID]]:
    row = conn.execute(
        "SELECT match_status, match_tier, rejected_file_ids FROM track_identities WHERE id = %s",
        (song,),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row["match_status"], row["match_tier"], list(row["rejected_file_ids"] or [])


def _matches(conn: Conn, song: UUID) -> list[tuple[UUID, str]]:
    rows = conn.execute(
        "SELECT library_file_id, match_tier FROM matches WHERE identity_id = %s ORDER BY id",
        (song,),
    ).fetchall()
    conn.commit()
    return [(row["library_file_id"], row["match_tier"]) for row in rows]


# Each write, as (status the song starts in, request). The file ids are filled in per case:
# "file" is the one the song is matched to (or, for Resolve, the one it is matched to by hand).
WRITES = {
    "resolve": (MatchStatus.NEEDS_REVIEW, "resolve", "manual_matched"),
    "reject": (MatchStatus.AUTO_MATCHED, "resolve", "manual_rejected"),
    "unmatch": (MatchStatus.AUTO_MATCHED, "unmatch", None),
}


def _post(client: TestClient, song: UUID, write: str, file_id: UUID | None) -> httpx.Response:
    _, action, status = WRITES[write]
    url = f"{API}/identities/{song}/{action}"
    if status is None:
        return client.post(url)
    body: dict[str, str] = {"match_status": status}
    if file_id is not None:
        body["library_file_id"] = str(file_id)
    return client.post(url, json=body)


def _seed(db_conn: Conn, write: str) -> tuple[UUID, UUID, UUID]:
    """A song in the write's starting status, matched to *old*; *new* is the file Resolve picks.
    Returns (song, old, new)."""
    song = _song(db_conn, WRITES[write][0])
    old, new = _file(db_conn, f"old-{uuid4()}"), _file(db_conn, f"new-{uuid4()}")
    _matched(db_conn, song, old)
    return song, old, new


@pytest.mark.parametrize("write", sorted(WRITES))
def test_a_request_opens_one_sync_connection(
    client: TestClient, db_conn: Conn, monkeypatch: pytest.MonkeyPatch, write: str
) -> None:
    song, old, new = _seed(db_conn, write)  # no work: Resolve's recalculation is skipped
    opened: list[str] = []
    real_connect = sync_conn.connect_sync

    def _counting(dsn: str, **kwargs: Any) -> psycopg.Connection[Any]:
        opened.append(dsn)
        return real_connect(dsn, **kwargs)

    monkeypatch.setattr(sync_conn, "connect_sync", _counting)

    response = _post(client, song, write, new if write == "resolve" else old)

    assert response.status_code == 200
    assert len(opened) == 1


def test_the_recalculation_sees_the_committed_match(
    client: TestClient, db_conn: Conn, migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    song = _song(db_conn, MatchStatus.NEEDS_REVIEW)
    work_id = work(RepositoryFactory(db_conn))
    db_conn.commit()
    picked = _file(db_conn, "picked", work_id)
    seen: list[tuple[str, list[tuple[UUID, str]]]] = []

    def _recalculate(db_url: str, w: str, repos_factory: object) -> None:
        with psycopg.connect(db_url, row_factory=dict_row) as other:
            seen.append((w, _matches(other, song)))

    monkeypatch.setattr("backend.routers.matching.recalculate_for_work_sync", _recalculate)

    response = _post(client, song, "resolve", picked)

    assert response.status_code == 200
    assert seen == [(work_id, [(picked, "manual")])]


@contextmanager
def _match_writes_refused(db_url: str) -> Iterator[None]:
    """Every insert into and delete from ``matches`` fails in the database until the block
    ends (as phase 0c's refused master writes)."""
    with psycopg.connect(db_url, autocommit=True) as admin:
        admin.execute(
            """CREATE OR REPLACE FUNCTION phase3a_refuse_match_write() RETURNS trigger
               LANGUAGE plpgsql AS $$
               BEGIN
                   RAISE EXCEPTION 'phase 3a: match write refused';
               END $$"""
        )
        admin.execute(
            """CREATE TRIGGER phase3a_refuse_match_write
               BEFORE INSERT OR DELETE ON matches
               FOR EACH ROW EXECUTE FUNCTION phase3a_refuse_match_write()"""
        )
        try:
            yield
        finally:
            admin.execute("DROP TRIGGER IF EXISTS phase3a_refuse_match_write ON matches")
            admin.execute("DROP FUNCTION IF EXISTS phase3a_refuse_match_write()")


@pytest.mark.parametrize("write", sorted(WRITES))
def test_a_refused_match_write_rolls_back_the_status(
    client: TestClient, db_conn: Conn, migrated_db: str, write: str
) -> None:
    song, old, new = _seed(db_conn, write)
    before = (_state(db_conn, song), _matches(db_conn, song))

    with _match_writes_refused(migrated_db):
        response = _post(client, song, write, new if write == "resolve" else old)

    assert response.status_code == 500
    assert (_state(db_conn, song), _matches(db_conn, song)) == before


def _wait_until_a_request_waits_on_a_lock(
    watch: Conn, request: threading.Thread, timeout_s: float = 4.0
) -> None:
    """Return once a backend of this database, other than *watch*, waits on a lock; fail if the
    request ends first or none does before the test database's 5 s lock_timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = watch.execute(
            """SELECT 1 FROM pg_stat_activity
               WHERE datname = current_database() AND pid <> pg_backend_pid()
                 AND wait_event_type = 'Lock'"""
        ).fetchone()
        if row is not None:
            return
        if not request.is_alive():
            pytest.fail("the request ended without waiting on the song's row lock")
        time.sleep(0.01)
    pytest.fail("the request never waited on the song's row lock")


# A worker's match, uncommitted, of a pending song: the request must wait for it, then
# decide on what it committed. Each case: (write, expected status, expected matches by file
# role, expected rejected files by file role).
LOCK_CASES = {
    "resolve": ("manual_matched", [("shown", "manual")], []),
    "reject": ("manual_rejected", [], ["worker"]),
    "unmatch": ("manual_rejected", [], ["worker"]),
}


@pytest.mark.parametrize("write", sorted(LOCK_CASES))
def test_the_song_row_is_locked_first(
    client: TestClient, db_conn: Conn, migrated_db: str, write: str
) -> None:
    """Reject and Unmatch lock the song's row before they read its status and matched files,
    and Resolve updates it before it deletes the song's matches, so a worker's uncommitted
    auto match is waited for and then rejected, or replaced by the manual one."""
    song = _song(db_conn, MatchStatus.PENDING)
    files = {"worker": _file(db_conn, "worker"), "shown": _file(db_conn, "shown")}
    outcome: list[httpx.Response] = []

    with (
        psycopg.connect(migrated_db, row_factory=dict_row) as worker,
        psycopg.connect(migrated_db, row_factory=dict_row, autocommit=True) as watch,
    ):
        worker.execute(
            "UPDATE track_identities SET match_status = %s, match_tier = %s WHERE id = %s",
            (MatchStatus.AUTO_MATCHED.value, MatchTier.VECTOR.value, song),
        )
        match(worker, song, files["worker"])
        request = threading.Thread(
            target=lambda: outcome.append(_post(client, song, write, files["shown"])),
            daemon=True,
        )
        request.start()
        try:
            _wait_until_a_request_waits_on_a_lock(watch, request)
            worker.commit()
        finally:
            if request.is_alive() and worker.info.transaction_status != TransactionStatus.IDLE:
                worker.rollback()  # never leave the request waiting past the test
            request.join(timeout=10)
        assert not request.is_alive()

    status, kept, rejected = LOCK_CASES[write]
    assert [r.status_code for r in outcome] == [200]
    assert outcome[0].json() == {"id": str(song), "match_status": status}
    assert _state(db_conn, song)[0] == status
    assert _matches(db_conn, song) == [(files[role], tier) for role, tier in kept]
    assert _state(db_conn, song)[2] == [files[role] for role in rejected]


def _lost_connection(db_url: str) -> Callable[[], Iterator[RepositoryFactory]]:
    def _repos() -> Iterator[RepositoryFactory]:
        conn = sync_conn.connect_sync(db_url)
        conn.close()
        yield RepositoryFactory(conn)

    return _repos


@pytest.mark.parametrize("write", sorted(WRITES))
def test_a_lost_connection_answers_503(client: TestClient, migrated_db: str, write: str) -> None:
    from backend.main import app

    app.dependency_overrides[get_sync_repos] = _lost_connection(migrated_db)
    try:
        response = _post(client, uuid4(), write, uuid4())
    finally:
        app.dependency_overrides.pop(get_sync_repos, None)

    assert (response.status_code, response.json()) == (503, {"detail": "unavailable"})

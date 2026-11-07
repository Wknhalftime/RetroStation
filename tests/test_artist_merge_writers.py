"""Who writes ``artist_merges``, and how (phase 2a S11; M33, C75; AUD-R041). Static checks, so the
fast suite runs them; the behaviour is in ``tests/integration/test_artist_merge_pg.py``.

- Nothing merges automatically. Only the adapter, ``backend/db/repositories/artist_merges.py``,
  holds SQL that writes the table: the scan reads every string literal of every ``.py`` file in
  ``backend/`` and ``scripts/``, and every ``.sql`` file there, migrations included, so a data
  migration that merged would show. Only ``backend/services/artist_merge_service.py`` calls the
  port's two writes, ``add_merge`` and ``remove_merge``. Only routes import that service, besides
  the two wiring modules (``backend/dependencies.py``, ``backend/services/repository_factory.py``),
  so no task or other service reaches it. A route is the curator's request.
- One SQL constant walks a merge to its root, for ``artist_group`` and the async Library routes.
  Only the adapter holds SQL that follows an edge's ``primary_id`` (the migrations aside); the
  artists router imports from the adapter's module. A route may still ask whether an artist is
  merged by its ``artist_id`` alone.
- Both write routes, ``POST`` and ``DELETE /api/v1/library/artists/{id}/merge``, are sync routes on
  ``SyncRepos`` (C75): one connection, one transaction.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from backend.dependencies import get_db_connection, get_sync_repos
from backend.repositories.artist_merges import ArtistMergeRepository
from backend.routers.library.artists import router as artists_router

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("backend", "scripts")
ADAPTER = "backend/db/repositories/artist_merges.py"
ADAPTER_MODULE = "backend.db.repositories.artist_merges"
SERVICE = "backend/services/artist_merge_service.py"
SERVICE_MODULE = "backend.services.artist_merge_service"
ROUTER = "backend/routers/library/artists.py"
WIRING = {"backend/dependencies.py", "backend/services/repository_factory.py"}
WRITES = {"add_merge", "remove_merge"}
SKIPPED_DIRS = {"__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}

_WRITES_THE_TABLE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO|TRUNCATE(?:\s+TABLE)?|COPY)"
    r"\s+(?:ONLY\s+)?(?:public\.)?\"?artist_merges\b",
    re.IGNORECASE,
)


def _files() -> Iterator[tuple[str, Path]]:
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*")):
            if path.suffix in {".py", ".sql"} and not SKIPPED_DIRS & set(path.parts):
                yield path.relative_to(ROOT).as_posix(), path


def _is_migration(relative: str) -> bool:
    return relative.startswith("backend/db/migrations/") or (
        re.fullmatch(r"backend/db/rollback_[^/]+\.sql", relative) is not None
    )


def _string_literals(tree: ast.AST) -> Iterator[str]:
    """Every string constant; an f-string's literal parts come one by one."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def _sql(relative: str, path: Path) -> list[str]:
    """The SQL a file can hold: a .sql file whole, a .py file's string literals."""
    text = path.read_text(encoding="utf-8")
    if relative.endswith(".sql"):
        return [text]
    return list(_string_literals(ast.parse(text)))


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return found


def _calls_a_write(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in WRITES
        for node in ast.walk(tree)
    )


def test_nothing_merges_automatically() -> None:
    assert ArtistMergeRepository.__abstractmethods__ >= WRITES

    writers: set[str] = set()
    callers: set[str] = set()
    importers: set[str] = set()
    for relative, path in _files():
        if any(_WRITES_THE_TABLE.search(sql) for sql in _sql(relative, path)):
            writers.add(relative)
        if relative.endswith(".py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if _calls_a_write(tree):
                callers.add(relative)
            if SERVICE_MODULE in _imports(tree):
                importers.add(relative)

    assert writers == {ADAPTER}
    assert callers == {SERVICE}
    assert ROUTER in importers
    assert all(p.startswith("backend/routers/") or p in WIRING for p in importers), importers


def _follows_an_edge(sql: str) -> bool:
    """SQL (not prose: it has an upper-case clause) that reads an edge's primary."""
    return "artist_merges" in sql and "primary_id" in sql and _CLAUSE.search(sql) is not None


_CLAUSE = re.compile(r"\b(?:SELECT|JOIN|WHERE|INSERT|DELETE|UPDATE)\b")


def test_one_walk_serves_the_resolver_and_the_routes() -> None:
    walkers = {
        relative
        for relative, path in _files()
        if not _is_migration(relative) and any(map(_follows_an_edge, _sql(relative, path)))
    }

    assert walkers == {ADAPTER}
    router_tree = ast.parse((ROOT / ROUTER).read_text(encoding="utf-8"))
    assert ADAPTER_MODULE in _imports(router_tree)


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

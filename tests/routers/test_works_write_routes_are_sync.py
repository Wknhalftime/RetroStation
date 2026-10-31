"""The works router's write routes are sync routes on SyncRepos (artist model phase 0c, SPEC S4;
C75).

Each write route is a plain ``def``, which FastAPI runs in a worker thread, on the request's
one sync connection from ``get_sync_repos``: through ``SyncRepos``'s single "function"-scoped,
cached dependency, it commits when the route succeeds and rolls back when it raises, so a
write is one transaction and two requests never share a connection (E2). No write route takes
the async ``get_db_connection``. Today's write routes keep their status codes and their
request and response bodies (field names, JSON types, required fields; not an array's item
type or a string's format); a phase that changes a body relocks this file. A later phase may
add write routes, which must be sync routes too. Each refusal's status and detail are pinned
in ``test_works_write_refusals.py``.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from backend.dependencies import get_db_connection, get_sync_repos
from backend.routers.library.works import router

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# A body as {field: JSON type}; a field the body may leave out ends in "?".
type Body = dict[str, str] | None

# (method, path) -> (success status, request body, response body), as today.
TODAYS_WRITE_ROUTES: dict[tuple[str, str], tuple[int, Body, Body]] = {
    ("PUT", "/works/{work_id}/master"): (
        200,
        {"preferred_file_id": "string"},
        {
            "id": "string",
            "work_id": "string",
            "preferred_file_id": "string",
            "selection_method": "string",
            "score": "integer|null",
            "updated_at": "string",
        },
    ),
    ("DELETE", "/works/{work_id}/master"): (204, None, None),
    ("POST", "/works/{work_id}/format-overrides"): (
        201,
        {"format_name": "string", "preferred_file_id": "string", "notes?": "string|null"},
        {
            "id": "string",
            "work_id": "string",
            "format_name": "string",
            "preferred_file_id": "string",
            "notes": "string|null",
            "created_at": "string",
        },
    ),
    ("DELETE", "/works/{work_id}/format-overrides/{override_id}"): (204, None, None),
    ("POST", "/works/{target_id}/merge"): (
        200,
        {"source_work_ids": "array"},
        {
            "merged_file_count": "integer",
            "deleted_work_count": "integer",
            "dropped_override_count": "integer",
        },
    ),
    ("POST", "/works/{work_id}/split"): (
        201,
        {"file_id": "string"},
        {"new_work_id": "string", "old_work_deleted": "boolean"},
    ),
    ("PATCH", "/files/{file_id}/work"): (
        200,
        {"work_id": "string"},
        {"old_work_id": "string|null", "old_work_deleted": "boolean"},
    ),
}


def _write_routes() -> dict[tuple[str, str], APIRoute]:
    return {
        (method, route.path): route
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods & WRITE_METHODS
    }


def _dependencies(dependant: Dependant) -> Iterator[Dependant]:
    for dependency in dependant.dependencies:
        yield dependency
        yield from _dependencies(dependency)


def _calls(dependant: Dependant) -> set[Callable[..., Any]]:
    return {d.call for d in _dependencies(dependant) if d.call is not None}


def test_todays_write_routes_are_all_there() -> None:
    assert sorted(set(TODAYS_WRITE_ROUTES) - set(_write_routes())) == []


def test_every_write_route_is_a_sync_route_on_sync_repos() -> None:
    routes = _write_routes()
    assert routes
    is_async = inspect.iscoroutinefunction
    assert sorted(key for key, route in routes.items() if is_async(route.endpoint)) == []
    calls = {key: _calls(route.dependant) for key, route in routes.items()}
    assert sorted(key for key, found in calls.items() if get_sync_repos not in found) == []
    assert sorted(key for key, found in calls.items() if get_db_connection in found) == []


def test_every_write_route_has_one_connection_one_transaction() -> None:
    """Every use of get_sync_repos in a route is the one cached, "function"-scoped SyncRepos:
    a second key (another scope, or no cache) would open a second connection."""
    for key, route in _write_routes().items():
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


def test_todays_bodies_and_status_codes_are_unchanged() -> None:
    app = FastAPI()
    app.include_router(router)
    spec = app.openapi()
    components = spec["components"]["schemas"]

    found: dict[tuple[str, str], tuple[int, Body, Body]] = {}
    for method, path in TODAYS_WRITE_ROUTES:
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

    assert found == TODAYS_WRITE_ROUTES

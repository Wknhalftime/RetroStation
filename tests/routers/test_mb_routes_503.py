"""A MusicBrainz error on the one route that calls MusicBrainz answers 503 (MusicBrainz cache fill,
phase 4.1 PR 1; SPEC S6; design C46; AUD-R042, AUD-R090).

``GET /api/v1/matching/mb-artists`` (the panel's "Search MusicBrainz") is the only API route that
calls MusicBrainz. Its client raised httpx's errors straight through, so an outage answered 500.
Now any MusicBrainz error the search raises (a 429 or 5xx after the client's retries, a timeout,
a network error, a dropped connection, or a 4xx) answers 503 "unavailable", as a lost database
connection already does. The router catches a domain error only
(``.claude/rules/architecture-boundaries.md``): a thin service turns httpx's error into
``MusicBrainzUnavailableError``, so the router never names httpx.

No database: the MusicBrainz client is replaced through ``get_mb_client``, as the locked
``test_matching.py::TestSearchMbArtists`` does.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from backend.dependencies import get_current_token, get_mb_client
from backend.routers.v1 import router as v1_router

MB_ARTISTS = "/api/v1/matching/mb-artists"
ROUTER = Path(__file__).resolve().parents[2] / "backend" / "routers" / "matching.py"


def _request() -> httpx.Request:
    return httpx.Request("GET", "https://musicbrainz.org/ws/2/artist/")


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = _request()
    return httpx.HTTPStatusError(
        f"{code}", request=request, response=httpx.Response(code, request=request)
    )


ERRORS: dict[str, BaseException] = {
    "503": _status_error(503),
    "500": _status_error(500),
    "429": _status_error(429),
    "400": _status_error(400),
    "timeout": httpx.ReadTimeout("timed out", request=_request()),
    "network": httpx.ConnectError("refused", request=_request()),
    "dropped": httpx.RemoteProtocolError("closed", request=_request()),
}


class _Client:
    """A MusicBrainz client whose search raises *error*, or answers *items*."""

    live_fetches = 0
    cache_hits = 0

    def __init__(self, error: BaseException | None = None, items: list[Any] | None = None):
        self.error = error
        self.items = items or []
        self.asked: list[str] = []

    def search_artist(self, name: str) -> list[Any]:
        self.asked.append(name)
        if self.error is not None:
            raise self.error
        return self.items


def _app(client: _Client) -> FastAPI:
    app = FastAPI()
    app.include_router(v1_router)
    app.dependency_overrides[get_current_token] = lambda: "test-token"

    def mb_client() -> Iterator[_Client]:
        yield client

    app.dependency_overrides[get_mb_client] = mb_client
    return app


async def _get(app: FastAPI, query: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.get(MB_ARTISTS, params={"query": query})


@pytest.mark.parametrize("error", list(ERRORS.values()), ids=list(ERRORS))
async def test_a_musicbrainz_error_is_503(error: BaseException) -> None:
    client = _Client(error=error)

    response = await _get(_app(client), "Blondie")

    assert client.asked == ["Blondie"]
    assert response.status_code == 503
    assert response.json() == {"detail": "unavailable"}


async def test_an_answer_is_still_the_items() -> None:
    hit = {"id": "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d", "name": "The Beatles", "score": 100}
    client = _Client(items=[hit])

    response = await _get(_app(client), "The Beatles")

    assert response.status_code == 200
    assert response.json() == {"items": [hit]}


def test_the_router_never_names_httpx() -> None:
    """R5: the translation lives below the router, which catches a domain error only."""
    tree = ast.parse(ROUTER.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "httpx" not in imported

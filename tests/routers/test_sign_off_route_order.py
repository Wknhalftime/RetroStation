"""The sign-off routes refuse before they open a database connection (G1 review, M5).

Requirements: H1 (``X-Airwave-Token`` required); M3 (an upload whose ``Content-Length`` is
over the limit is refused before its body is read). A refused request must not take a
database connection: ``get_sync_repos`` is replaced by a recorder, and the routes' real
``get_sign_off_ports`` and token check run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from backend.dependencies import get_current_token, get_sync_repos
from backend.domain.streaming import MAX_CLIP_BYTES
from backend.routers.v1 import router as v1_router
from tests.fakes.user_settings import FakeUserSettingRepository

SIGN_OFF = "/api/v1/streaming/sign-off"
BOUNDARY = "g1-order-boundary"


class Connections:
    """Counts the database connections the routes ask for (none should be opened)."""

    def __init__(self) -> None:
        self.opened = 0

    def __call__(self) -> Iterator[SimpleNamespace]:
        self.opened += 1
        yield SimpleNamespace(user_settings=FakeUserSettingRepository(), commit=lambda: None)


def app_for(connections: Connections, *, token: bool) -> FastAPI:
    app = FastAPI()
    app.include_router(v1_router)
    app.dependency_overrides[get_sync_repos] = connections
    if token:
        app.dependency_overrides[get_current_token] = lambda: "test-token"
    return app


async def _never_read() -> AsyncIterator[bytes]:
    raise AssertionError("the body must not be read")
    yield b""  # pragma: no cover - makes this an async generator


async def send(app: FastAPI, method: str, *, declared: int | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        if declared is None:
            return await http.request(method, SIGN_OFF)
        headers = {
            "content-type": f"multipart/form-data; boundary={BOUNDARY}",
            "content-length": str(declared),
        }
        return await http.request(method, SIGN_OFF, content=_never_read(), headers=headers)


@pytest.mark.parametrize("method", ["POST", "DELETE"])
async def test_a_request_without_the_token_opens_no_connection(method: str) -> None:
    connections = Connections()
    response = await send(app_for(connections, token=False), method)
    assert response.status_code == 401
    assert connections.opened == 0


async def test_an_upload_declared_too_large_opens_no_connection() -> None:
    connections = Connections()
    declared = MAX_CLIP_BYTES + 2 * 2**20
    response = await send(app_for(connections, token=True), "POST", declared=declared)
    assert response.status_code == 413
    assert connections.opened == 0

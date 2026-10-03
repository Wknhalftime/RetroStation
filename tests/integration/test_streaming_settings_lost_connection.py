"""The settings routes answer 503 when the database connection dies inside a settings read or
write (follow-up to PR G1 #132, confirmed with its coordinator; PR #133 item 1).

Requirements: G1 final review (a lost connection in a user-settings read or upsert answered
500). The user-settings repository raises ``StorageUnavailableError`` for it, and each route
maps that to 503: ``GET /streaming/settings``, ``PUT /streaming/max-sessions``, the
sign-off's ``POST`` and ``DELETE`` (whose settings read comes before the commit G1 already
maps, T3.29), and the generic ``PUT /settings/{key}``. Never a 422: a lost connection is
not a refused setting. The dependencies are the real ones on the real PostgreSQL test
database; only the token is bypassed. Each request's backend is terminated with
``pg_terminate_backend`` from a second connection right after it connects.
"""

from __future__ import annotations

import io
import wave
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from fastapi import FastAPI

import backend.db.sync_conn as sync_conn
from backend.config import get_settings
from backend.dependencies import get_current_token
from backend.routers.v1 import router as v1_router

pytestmark = pytest.mark.integration

RATE = 8_000


def _wav(seconds: float) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(b"\x00\x00" * round(seconds * RATE))
    return buffer.getvalue()


@pytest.fixture
def app(migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("STREAM_WORK_DIR", str(tmp_path / "stream-work"))
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
        conn = real(dsn, **kwargs)
        with psycopg.connect(migrated_db, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (conn.info.backend_pid,))
        killed.append(conn.info.backend_pid)
        return conn

    monkeypatch.setattr(sync_conn, "connect_sync", connect)
    return killed


ROUTES: dict[str, tuple[str, str, dict[str, Any]]] = {
    "read_streaming_settings": ("GET", "/api/v1/streaming/settings", {}),
    "save_max_sessions": ("PUT", "/api/v1/streaming/max-sessions", {"json": {"value": 3}}),
    "upload_sign_off": (
        "POST",
        "/api/v1/streaming/sign-off",
        {"files": {"file": ("lost.wav", _wav(2.0), "application/octet-stream")}},
    ),
    "remove_sign_off": ("DELETE", "/api/v1/streaming/sign-off", {}),
    "save_setting": ("PUT", "/api/v1/settings/ui_theme", {"json": {"value": "dark"}}),
}


@pytest.mark.parametrize(("method", "url", "send"), ROUTES.values(), ids=ROUTES.keys())
async def test_a_connection_lost_in_a_settings_route_is_503(
    app: FastAPI,
    connections_die: list[int],
    method: str,
    url: str,
    send: dict[str, Any],
) -> None:
    # raise_app_exceptions: an error in the dependency's teardown fails the test outright.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.request(method, url, **send)
    assert len(connections_die) == 1
    assert response.status_code == 503
    assert response.json() == {"detail": "unavailable"}

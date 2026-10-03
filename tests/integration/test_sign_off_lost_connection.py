"""The sign-off routes when the database connection dies during the commit (G1 review, I1).

Requirements: I3 (storage failures answer 503, on POST and on DELETE, with the real ports);
"never skip error handling" (a lost connection is a 503, never a 500). The dependencies are the
real ones (``get_sync_repos``, ``get_sign_off_ports``, ``get_sign_off_folder``) on the real
PostgreSQL test database; only the token is bypassed. The request's backend is terminated with
``pg_terminate_backend`` from a second connection just before ``RepositoryFactory.commit``
runs, so the commit itself meets the dead connection, as in production.
"""

from __future__ import annotations

import io
import wave
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from backend.config import get_settings
from backend.dependencies import get_current_token
from backend.routers.v1 import router as v1_router
from backend.services.repository_factory import RepositoryFactory

pytestmark = pytest.mark.integration

SIGN_OFF = "/api/v1/streaming/sign-off"
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
def commit_on_a_dead_connection(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every ``RepositoryFactory.commit`` first has its backend terminated; returns the pids."""
    real_commit: Callable[[RepositoryFactory], None] = RepositoryFactory.commit
    killed: list[int] = []

    def commit(self: RepositoryFactory) -> None:
        pid = self._conn.info.backend_pid
        with psycopg.connect(migrated_db, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        killed.append(pid)
        real_commit(self)

    monkeypatch.setattr(RepositoryFactory, "commit", commit)
    return killed


async def _send(app: FastAPI, method: str, upload: bytes | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        if upload is None:
            return await http.request(method, SIGN_OFF)
        files = {"file": ("lost.wav", upload, "application/octet-stream")}
        return await http.request(method, SIGN_OFF, files=files)


async def test_a_connection_lost_in_the_upload_commit_is_503(
    app: FastAPI, commit_on_a_dead_connection: list[int]
) -> None:
    response = await _send(app, "POST", _wav(2.0))
    assert len(commit_on_a_dead_connection) == 1
    assert response.status_code == 503


async def test_a_connection_lost_in_the_remove_commit_is_503(
    app: FastAPI, commit_on_a_dead_connection: list[int]
) -> None:
    response = await _send(app, "DELETE")
    assert len(commit_on_a_dead_connection) == 1
    assert response.status_code == 503

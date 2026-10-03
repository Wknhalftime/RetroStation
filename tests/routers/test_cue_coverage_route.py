"""The cue coverage route over HTTP (PR G2, Task 6; traceability M: T6.8-T6.10).

Requirements: D77 and D89 (the Streaming page shows "cues ready X of Y"); PG7; D51 and manual
check 8 (cue analysis, and so its count, works whether or not streaming is on); H1 (under
``/api/v1``, ``X-Airwave-Token`` required). The plan's route table: ``GET
/api/v1/streaming/cue-coverage`` answers ``{"analysable", "ready", "failed", "unhashed"}``.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from backend.config import get_settings
from backend.dependencies import get_cue_coverage, get_current_token
from backend.domain.library import AudioHash
from backend.domain.streaming import CUE_ANALYSER_VERSION, CueAnalysis, CuePoints
from backend.routers.v1 import router as v1_router
from tests.fakes.stream_cue_coverage import FakeCueCoverageRepository
from tests.fakes.stream_cues import FakeStreamCueRepository

COVERAGE = "/api/v1/streaming/cue-coverage"
POINTS = CuePoints(
    cue_in_ms=0, cue_out_ms=180_000, fade_in_ms=0, fade_out_ms=0, start_next_ms=0, gain_db=0.0
)


def audio() -> AudioHash:
    return AudioHash.parse(f"flac-md5:{uuid4().hex}")


def stored(store: FakeStreamCueRepository, audio_hash: AudioHash, *, failed: bool) -> None:
    store.upsert(
        CueAnalysis(
            audio_hash=audio_hash,
            cues=POINTS,
            loudness_lufs=None,
            analysis_failed=failed,
            analyser_version=CUE_ANALYSER_VERSION,
        )
    )


def library() -> FakeCueCoverageRepository:
    """Four analysable audio (one ready, one failed, two waiting) and two unhashed files."""
    store = FakeStreamCueRepository()
    repo = FakeCueCoverageRepository(store)
    ready, failed = audio(), audio()
    for audio_hash in (ready, failed, audio(), audio()):
        repo.add(uuid4(), audio_hash)
    repo.add(uuid4(), None)
    repo.add(uuid4(), None)
    stored(store, ready, failed=False)
    stored(store, failed, failed=True)
    return repo


def app_for(repo: FakeCueCoverageRepository, *, token: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(v1_router)
    app.dependency_overrides[get_cue_coverage] = lambda: repo
    if token:
        app.dependency_overrides[get_current_token] = lambda: "test-token"
    return app


async def get(app: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.get(COVERAGE)


async def test_the_coverage_is_json_in_the_pages_shape() -> None:
    # T6.8 (D77, D89, PG7): exactly the four counts.
    response = await get(app_for(library()))
    assert response.status_code == 200
    assert response.json() == {"analysable": 4, "ready": 1, "failed": 1, "unhashed": 2}


async def test_the_coverage_is_served_while_streaming_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # T6.9 (D51, manual check 8): the count does not depend on the stream service.
    monkeypatch.setenv("STREAM_ENABLED", "false")
    get_settings.cache_clear()
    try:
        app = app_for(library())
        app.state.stream_service = None
        response = await get(app)
    finally:
        get_settings.cache_clear()
    assert (response.status_code, response.json()["analysable"]) == (200, 4)


async def test_the_coverage_route_needs_the_token() -> None:
    # T6.10 (H1): no token, no count, and the repository is never read.
    repo = library()
    response = await get(app_for(repo, token=False))
    assert response.status_code == 401
    assert repo.reads == 0

"""The cue coverage route answers 503, not 500, when the count cannot be read (PR G2, Task 6
fix; coordinator review).

Requirements: D88 (``StreamReadError`` -> 503 ``unavailable``, as ``listen.py``'s table and the
sibling streaming-settings routes already answer a lost connection); the dependency must raise
inside the route's own call, not during dependency resolution, so the route's try/except can
map it (a generator dependency that opens its connection before yielding raises too early for
that).
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from backend.dependencies import get_cue_coverage, get_current_token
from backend.domain.streaming import CueCoverage, StreamReadError
from backend.repositories.stream_cue_coverage import CueCoverageRepository
from backend.routers.v1 import router as v1_router

COVERAGE = "/api/v1/streaming/cue-coverage"


class _UnavailableCoverage(CueCoverageRepository):
    """Raises on every read, as a locked or unreachable database would (D88)."""

    def coverage(self) -> CueCoverage:
        raise StreamReadError("stream_cues: canceling statement due to lock timeout")


def app_for(repo: CueCoverageRepository) -> FastAPI:
    app = FastAPI()
    app.include_router(v1_router)
    app.dependency_overrides[get_cue_coverage] = lambda: repo
    app.dependency_overrides[get_current_token] = lambda: "test-token"
    return app


async def test_an_unreadable_coverage_answers_503_not_500() -> None:
    app = app_for(_UnavailableCoverage())
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get(COVERAGE)
    assert response.status_code == 503
    assert response.json() == {"detail": "unavailable"}

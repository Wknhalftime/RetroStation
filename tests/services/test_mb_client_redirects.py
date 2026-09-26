"""Merged MBIDs: MusicBrainz answers 301 to the surviving entity.

Until now the client did not follow redirects, so the lookup raised and
the files behind a merged release were retried, and failed, on every
enrichment run.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

import backend.services.mb_client as mb_mod
from backend.services.mb_client import MusicBrainzApiClient
from tests.fakes.musicbrainz_cache import FakeMusicBrainzCacheRepository

OLD = "b07b4d2f-bd70-46c7-b208-2d9438fc1511"
NEW = "e572bae7-fb00-4863-87fc-78dfd0ab091c"


def _redirecting_server(entity: str) -> tuple[Any, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == f"/ws/2/{entity}/{OLD}":
            return httpx.Response(
                301, headers={"Location": f"https://musicbrainz.org/ws/2/{entity}/{NEW}?fmt=json"},
            )
        if request.url.path == f"/ws/2/{entity}/{NEW}":
            return httpx.Response(200, json={"id": NEW, "title": "Survivor", "name": "Survivor"})
        return httpx.Response(404, json={"error": "Not Found"})

    return handler, seen


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mb_mod, "_rate_limit", lambda: None)


@pytest.fixture
def cache() -> FakeMusicBrainzCacheRepository:
    return FakeMusicBrainzCacheRepository()


@pytest.mark.parametrize("entity,method", [
    ("release", "lookup_release"),
    ("recording", "lookup_recording"),
    ("artist", "lookup_artist"),
])
def test_lookup_follows_a_merged_mbid_to_the_survivor(
    cache: FakeMusicBrainzCacheRepository, entity: str, method: str,
) -> None:
    handler, seen = _redirecting_server(entity)
    with MusicBrainzApiClient(cache, transport=httpx.MockTransport(handler)) as client:
        payload = getattr(client, method)(OLD)

    assert payload is not None
    assert payload["id"] == NEW
    assert seen == [f"/ws/2/{entity}/{OLD}", f"/ws/2/{entity}/{NEW}"]
    # Cached under the MBID that was asked for, so the next run is a hit.
    assert cache.get(f"{entity}:{OLD}") is not None
    assert client.live_fetches == 1

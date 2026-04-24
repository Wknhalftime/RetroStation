from __future__ import annotations

import httpx

from backend.services.mb_client import MusicBrainzApiClient
from tests.fakes.musicbrainz_cache import FakeMusicBrainzCacheRepository


class _StubResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def test_lookup_artist_caches_response(monkeypatch):
    """Second call must hit cache, not HTTP."""
    cache = FakeMusicBrainzCacheRepository()
    calls = {"n": 0}
    payload = {
        "id": "mbid-X",
        "name": "Nick Cave",
        "sort-name": "Cave, Nick",
        "disambiguation": "Australian musician",
    }

    def fake_fetch(self, url, params):
        calls["n"] += 1
        return _StubResponse(payload)

    monkeypatch.setattr(MusicBrainzApiClient, "_fetch", fake_fetch)

    with MusicBrainzApiClient(cache) as client:
        first = client.lookup_artist("mbid-X")
        second = client.lookup_artist("mbid-X")

    assert first is not None
    assert first["name"] == "Nick Cave"
    assert second == first
    assert calls["n"] == 1  # second call served from cache


def test_lookup_artist_404_returns_none(monkeypatch):
    cache = FakeMusicBrainzCacheRepository()

    def fake_fetch(self, url, params):
        request = httpx.Request("GET", url)
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("404", request=request, response=response)

    monkeypatch.setattr(MusicBrainzApiClient, "_fetch", fake_fetch)

    with MusicBrainzApiClient(cache) as client:
        assert client.lookup_artist("mbid-missing") is None

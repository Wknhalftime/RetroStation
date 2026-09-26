"""MusicBrainzApiClient.search_recordings_by_mbids: many recordings per call.

One search request carries up to 100 MBIDs as ``rid:(a OR b ...)``; each
recording that comes back is cached on its own so a later batch that
contains it makes no request for it.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

import backend.services.mb_client as mb_mod
from backend.services.mb_client import MusicBrainzApiClient
from tests.fakes.musicbrainz_cache import FakeMusicBrainzCacheRepository


def _mbid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


def _recording(mbid: str) -> dict[str, Any]:
    return {"id": mbid, "title": f"Track {mbid[:8]}", "length": 1000, "score": 100}


class _SearchServer:
    """Answers recording searches from a fixed set; records each request."""

    def __init__(self, known: list[str]) -> None:
        self.known = set(known)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        query = parse_qs(request.url.query.decode())["query"][0]
        asked = query[len("rid:("):-1].split(" OR ")
        found = [_recording(m) for m in asked if m in self.known]
        return httpx.Response(200, json={"count": len(found), "recordings": found})


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mb_mod, "_rate_limit", lambda: None)


@pytest.fixture
def cache() -> FakeMusicBrainzCacheRepository:
    return FakeMusicBrainzCacheRepository()


@pytest.fixture
def make_client(
    cache: FakeMusicBrainzCacheRepository,
) -> Iterator[Any]:
    clients: list[MusicBrainzApiClient] = []

    def factory(server: _SearchServer, **kwargs: Any) -> MusicBrainzApiClient:
        client = MusicBrainzApiClient(cache, **kwargs)
        client._http = httpx.Client(transport=httpx.MockTransport(server.handler))
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


def test_one_request_per_hundred_mbids_with_or_query(make_client: Any) -> None:
    mbids = [_mbid(n) for n in range(150)]
    server = _SearchServer(mbids)
    client = make_client(server)

    found = client.search_recordings_by_mbids(mbids)

    assert len(server.requests) == 2
    first = parse_qs(server.requests[0].url.query.decode())
    assert first["limit"] == ["100"]
    assert first["fmt"] == ["json"]
    assert first["query"][0] == "rid:(" + " OR ".join(mbids[:100]) + ")"
    assert set(found) == set(mbids)
    assert found[mbids[7]]["title"] == f"Track {mbids[7][:8]}"
    assert client.live_fetches == 2


def test_unknown_mbids_are_absent_from_the_result(make_client: Any) -> None:
    known, merged = _mbid(1), _mbid(2)
    client = make_client(_SearchServer([known]))

    found = client.search_recordings_by_mbids([known, merged])

    assert set(found) == {known}


def test_cached_recordings_are_not_requested_again(
    make_client: Any, cache: FakeMusicBrainzCacheRepository,
) -> None:
    mbids = [_mbid(n) for n in range(3)]
    server = _SearchServer(mbids)
    client = make_client(server)
    client.search_recordings_by_mbids(mbids)

    again = client.search_recordings_by_mbids([*mbids, _mbid(9)])

    # Only the new MBID goes to the network; the three known ones come from
    # the cache and are still in the result.
    assert len(server.requests) == 2
    last = parse_qs(server.requests[-1].url.query.decode())["query"][0]
    assert last == f"rid:({_mbid(9)})"
    assert set(again) == set(mbids)
    assert client.cache_hits == 3
    assert cache.get(f"recording-by-mbid:{mbids[0]}") is not None


def test_empty_input_makes_no_request(make_client: Any) -> None:
    server = _SearchServer([])
    client = make_client(server)

    assert client.search_recordings_by_mbids([]) == {}
    assert server.requests == []


def test_cache_ttl_is_configurable(
    make_client: Any, cache: FakeMusicBrainzCacheRepository,
) -> None:
    mbid = _mbid(1)
    client = make_client(_SearchServer([mbid]), ttl_days=3650)

    client.search_recordings_by_mbids([mbid])

    entry = cache.get(f"recording-by-mbid:{mbid}")
    assert entry is not None
    assert entry.expires_at - entry.cached_at == timedelta(days=3650)


def test_malformed_mbids_never_reach_the_query(make_client: Any) -> None:
    # One bad token would make MusicBrainz reject the whole batch.
    good = _mbid(1)
    server = _SearchServer([good])
    client = make_client(server)

    found = client.search_recordings_by_mbids(["not a uuid", good])

    assert set(found) == {good}
    query = parse_qs(server.requests[0].url.query.decode())["query"][0]
    assert query == f"rid:({good})"


def test_mbids_the_search_never_returns_are_not_asked_again(make_client: Any) -> None:
    # Merged MBIDs are absent from every search; without a negative entry a
    # warm run would re-issue one request per batch that contains one.
    known, merged = _mbid(1), _mbid(2)
    server = _SearchServer([known])
    client = make_client(server)
    client.search_recordings_by_mbids([known, merged])

    again = client.search_recordings_by_mbids([known, merged])

    assert len(server.requests) == 1
    assert set(again) == {known}


def test_cached_recordings_are_read_in_one_call_per_batch(
    make_client: Any, cache: FakeMusicBrainzCacheRepository,
) -> None:
    mbids = [_mbid(n) for n in range(5)]
    client = make_client(_SearchServer(mbids))
    client.search_recordings_by_mbids(mbids)
    cache.reads = 0

    client.search_recordings_by_mbids(mbids)

    assert cache.reads == 1


def test_cache_keeps_only_the_fields_enrichment_reads(
    make_client: Any, cache: FakeMusicBrainzCacheRepository,
) -> None:
    # A raw search hit is ~30 KB (every release the recording appears on,
    # aliases, tags); reading 100 of those per batch costs more than the
    # network did. Store just what enrich_by_recording_batch consumes.
    mbid = _mbid(1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 1, "recordings": [{
            "id": mbid, "title": "Song", "length": 1234, "score": 100, "video": None,
            "tags": [{"name": "rock"}], "isrcs": ["USXXX"], "first-release-date": "1999",
            "artist-credit": [{"name": "A", "artist": {
                "id": "a1", "name": "A", "sort-name": "A", "aliases": [{"name": "AA"}],
            }}],
            "releases": [{"id": "r1", "title": "R", "media": [{"format": "CD"}],
                          "release-events": [{"date": "1999"}]}],
        }]})

    client = MusicBrainzApiClient(cache)
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    found = client.search_recordings_by_mbids([mbid])

    slim = {
        "id": mbid, "title": "Song", "length": 1234,
        "artist-credit": [{"name": "A", "artist": {"id": "a1", "name": "A", "sort-name": "A"}}],
        "releases": [{"id": "r1"}],
    }
    entry = cache.get(f"recording-by-mbid:{mbid}")
    assert entry is not None
    assert entry.response_data == slim
    assert found[mbid] == slim

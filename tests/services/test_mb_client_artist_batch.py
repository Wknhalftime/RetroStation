"""MusicBrainzApiClient.search_artists_by_mbids: many artists per call.

One search request carries up to 100 MBIDs as ``arid:(a OR b ...)``; each
artist that comes back is cached on its own, in the same slim shape
``lookup_artist`` caches, so either path reads the other's rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from backend.services.mb_client import MusicBrainzApiClient, _slim_artist
from backend.services.mb_types import MbArtist
from tests.fakes.musicbrainz_cache import FakeMusicBrainzCacheRepository


def _mbid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


def _artist(mbid: str) -> dict[str, Any]:
    return {
        "id": mbid,
        "name": f"Artist {mbid[:8]}",
        "sort-name": f"Artist, {mbid[:8]}",
        "score": 100,
        "tags": [{"name": "rock", "count": 1}],
    }


class _SearchServer:
    """Answers artist searches from a fixed set; records each request."""

    def __init__(self, known: list[str]) -> None:
        self.known = set(known)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        query = parse_qs(request.url.query.decode())["query"][0]
        asked = query[len("arid:(") : -1].split(" OR ")
        found = [_artist(m) for m in asked if m in self.known]
        return httpx.Response(200, json={"count": len(found), "artists": found})

    def asked(self, index: int) -> list[str]:
        query = parse_qs(self.requests[index].url.query.decode())["query"][0]
        return query[len("arid:(") : -1].split(" OR ")


@pytest.fixture
def cache() -> FakeMusicBrainzCacheRepository:
    return FakeMusicBrainzCacheRepository()


@pytest.fixture
def make_client(cache: FakeMusicBrainzCacheRepository) -> Iterator[Any]:
    clients: list[MusicBrainzApiClient] = []

    def factory(server: _SearchServer, **kwargs: Any) -> MusicBrainzApiClient:
        client = MusicBrainzApiClient(cache, wait_turn=lambda: None, **kwargs)
        client._http = httpx.Client(transport=httpx.MockTransport(server.handler))
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


def test_one_request_per_hundred_ids(make_client: Any) -> None:
    mbids = [_mbid(n) for n in range(101)]
    server = _SearchServer(mbids)
    client = make_client(server)

    found = client.search_artists_by_mbids(mbids)

    assert len(server.requests) == 2
    assert server.asked(0) == mbids[:100]
    assert server.asked(1) == mbids[100:]
    assert set(found) == set(mbids)
    assert client.live_fetches == 2


def test_empty_list_makes_no_request_and_no_cache_read(
    make_client: Any, cache: FakeMusicBrainzCacheRepository
) -> None:
    server = _SearchServer([])
    client = make_client(server)
    reads: list[list[str]] = []
    original = cache.get_many
    cache.get_many = lambda keys: (reads.append(list(keys)), original(keys))[1]  # type: ignore[method-assign]

    assert client.search_artists_by_mbids([]) == {}

    assert server.requests == []
    assert reads == []


def test_cached_artist_is_not_asked_again(make_client: Any) -> None:
    mbid = _mbid(1)
    server = _SearchServer([mbid])
    client = make_client(server)
    client.search_artists_by_mbids([mbid])

    again = client.search_artists_by_mbids([mbid])

    assert len(server.requests) == 1
    assert client.cache_hits == 1
    assert set(again) == {mbid}


def test_lookup_artist_hits_the_batch_cache(make_client: Any) -> None:
    mbid = _mbid(1)
    server = _SearchServer([mbid])
    client = make_client(server)
    client.search_artists_by_mbids([mbid])

    artist = client.lookup_artist(mbid)

    assert len(server.requests) == 1
    assert artist == {"id": mbid, "name": f"Artist {mbid[:8]}", "sort-name": f"Artist, {mbid[:8]}"}


def test_unknown_id_is_absent_and_not_cached(
    make_client: Any, cache: FakeMusicBrainzCacheRepository
) -> None:
    mbid = _mbid(1)
    client = make_client(_SearchServer([]))

    assert client.search_artists_by_mbids([mbid]) == {}
    assert cache.get(f"artist:{mbid}") is None


def test_malformed_id_is_skipped(make_client: Any) -> None:
    good = _mbid(1)
    server = _SearchServer([good])
    client = make_client(server)

    found = client.search_artists_by_mbids(["not-an-mbid", good])

    assert len(server.requests) == 1
    assert server.asked(0) == [good]
    assert set(found) == {good}


def test_slim_artist_keeps_the_four_fields() -> None:
    hit = {
        "id": "x",
        "name": "N",
        "sort-name": "N, S",
        "disambiguation": "",
        "score": 100,
        "tags": [{"name": "rock", "count": 1}],
        "area": {"id": "a", "name": "Somewhere"},
    }

    assert _slim_artist(hit) == {"id": "x", "name": "N", "sort-name": "N, S"}  # type: ignore[arg-type]


def test_slim_artist_keeps_a_non_empty_disambiguation() -> None:
    hit: MbArtist = {"id": "x", "name": "N", "sort-name": "N", "disambiguation": "UK band"}

    assert _slim_artist(hit)["disambiguation"] == "UK band"


def test_on_progress_once_per_mbid_in_input_order(make_client: Any) -> None:
    a, b, c = _mbid(1), _mbid(2), _mbid(3)
    server = _SearchServer([a, b, c])
    client = make_client(server)
    client.search_artists_by_mbids([a, b])
    calls: list[tuple[int, int, str]] = []

    client.search_artists_by_mbids(
        [a, b, c], on_progress=lambda i, total, mbid: calls.append((i, total, mbid))
    )

    assert calls == [(1, 3, a), (2, 3, b), (3, 3, c)]
    assert len(server.requests) == 2

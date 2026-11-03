"""MusicBrainzApiClient.lookup_recordings: many recordings, one lookup each.

Unlike ``search_artists_by_mbids`` this answers for every MBID it could ask about:
a 404 is a stored ``None`` (authoritative "not found"); only a transient failure
leaves the key out, so the caller can try again.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from tenacity import wait_none

from backend.services.mb_client import MusicBrainzApiClient
from tests.fakes.musicbrainz_cache import FakeMusicBrainzCacheRepository


class _RecordingServer:
    """Answers ``/recording/{id}``: known ids 200, ``broken`` ids 503, the rest 404."""

    def __init__(self, known: list[str], broken: list[str] | None = None) -> None:
        self.known = set(known)
        self.broken = set(broken or [])
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        mbid = request.url.path.rsplit("/", 1)[-1]
        if mbid in self.broken:
            return httpx.Response(503)
        if mbid in self.known:
            return httpx.Response(200, json={"id": mbid, "title": f"title-{mbid}"})
        return httpx.Response(404)


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    # A 503 is retried three times; do not wait out the backoff.
    monkeypatch.setattr(MusicBrainzApiClient._fetch.retry, "wait", wait_none())  # type: ignore[attr-defined]
    clients: list[MusicBrainzApiClient] = []

    def factory(server: _RecordingServer) -> MusicBrainzApiClient:
        client = MusicBrainzApiClient(FakeMusicBrainzCacheRepository(), wait_turn=lambda: None)
        client._http = httpx.Client(transport=httpx.MockTransport(server.handler))
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


def test_dedupes_and_calls_once_per_mbid(make_client: Any) -> None:
    server = _RecordingServer(["a", "b"])
    client = make_client(server)

    result = client.lookup_recordings(["a", "a", "b"])

    assert len(server.requests) == 2
    assert set(result) == {"a", "b"}
    assert result["a"] == {"id": "a", "title": "title-a"}


def test_404_is_stored_as_none(make_client: Any) -> None:
    client = make_client(_RecordingServer([]))

    assert client.lookup_recordings(["gone"]) == {"gone": None}


def test_transient_error_omits_the_key(make_client: Any) -> None:
    server = _RecordingServer(["ok"], broken=["flaky"])
    client = make_client(server)

    result = client.lookup_recordings(["flaky", "ok"])

    assert "flaky" not in result
    assert result["ok"] == {"id": "ok", "title": "title-ok"}


def test_on_progress_once_per_mbid(make_client: Any) -> None:
    client = make_client(_RecordingServer(["a", "b"]))
    seen: list[tuple[int, int, str]] = []

    client.lookup_recordings(["a", "a", "b"], on_progress=lambda i, n, m: seen.append((i, n, m)))

    assert seen == [(1, 2, "a"), (2, 2, "b")]

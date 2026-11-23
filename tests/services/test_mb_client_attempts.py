"""A one-attempt view of the MusicBrainz client (phase 4.1 PR 2b; SPEC S8; D9, D10; AUD-R093).

PR 2's gate (part B) sent 4,218 requests for 2,276 fill calls: the shared ``_fetch`` tries a call
3 times on a 429 or 5xx, and MusicBrainz refused 61% of requests. Re-asking a refused query 2–4 s
later was refused 77–90% of the time, so Lance ruled D10, "One try + 10 s pause": the fill asks
each call once and a refused key waits for the next run.

- ``client.with_attempts(n)`` is a view of the client whose calls stop after *n* attempts. It
  shares the client's cache, transport and clock (AUD-R102's one door), and leaves the client it
  came from as it was.
- ``_fetch``'s own decorator is untouched: it still makes 3 tries, so every other caller (the
  artist stage, song matching, enrichment) keeps its retries. Only the fill takes a view.
- ``requests_sent`` counts every attempt a client or view makes, refused ones included, so a
  fill run reports requests, not calls (D9).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from tenacity import wait_none

from backend.services.mb_client import MusicBrainzApiClient
from tests.fakes.musicbrainz_cache import FakeMusicBrainzCacheRepository

BACKEND = Path(__file__).resolve().parents[2] / "backend"
EMPTY = {"count": 0, "recordings": []}


class _Server:
    """MusicBrainz's recording search: each request takes the next status, then answers 200."""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses.pop(0) if self.statuses else 200
        if status == 200:
            return httpx.Response(200, json=EMPTY, request=request)
        return httpx.Response(status, request=request)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    # The client's own retries (3 tries) don't wait out their 2–30 s backoff here.
    retry = MusicBrainzApiClient._fetch.retry  # type: ignore[attr-defined]
    monkeypatch.setattr(retry, "wait", wait_none())


@pytest.fixture
def server() -> _Server:
    return _Server()


@pytest.fixture
def client(server: _Server) -> Iterator[MusicBrainzApiClient]:
    with MusicBrainzApiClient(
        FakeMusicBrainzCacheRepository(),
        transport=httpx.MockTransport(server),
        wait_turn=lambda: None,
    ) as mb:
        yield mb


def _search(client: MusicBrainzApiClient, title: str) -> None:
    client.search_recordings_by_name_title(["ALPHA"], title)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_the_client_itself_still_tries_three_times(
    client: MusicBrainzApiClient, server: _Server, status: int
) -> None:
    """D10: the artist stage and song matching keep 3 tries; the client is unchanged."""
    server.statuses = [status] * 3

    with pytest.raises(httpx.HTTPStatusError):
        _search(client, "Song 1")

    assert len(server.requests) == 3


@pytest.mark.parametrize("attempts", [1, 2])
def test_a_view_stops_after_its_attempts(
    client: MusicBrainzApiClient, server: _Server, attempts: int
) -> None:
    """S8: a view asks a refused call at most *attempts* times, then raises as the client does."""
    server.statuses = [503] * 3
    view = client.with_attempts(attempts)

    with pytest.raises(httpx.HTTPStatusError):
        _search(view, "Song 1")

    assert len(server.requests) == attempts


def test_a_view_answers_and_caches_as_its_client(
    client: MusicBrainzApiClient, server: _Server
) -> None:
    """S8: a view's answer lands in the client's cache, so the client reads it with no request."""
    view = client.with_attempts(1)

    _search(view, "Song 1")
    _search(client, "Song 1")

    assert len(server.requests) == 1


def test_a_view_leaves_its_client_as_it_was(client: MusicBrainzApiClient, server: _Server) -> None:
    """D10: taking a one-attempt view doesn't change the client it came from."""
    view = client.with_attempts(1)
    server.statuses = [503] * 4

    with pytest.raises(httpx.HTTPStatusError):
        _search(view, "Song 1")
    with pytest.raises(httpx.HTTPStatusError):
        _search(client, "Song 2")

    assert len(server.requests) == 1 + 3


def test_requests_sent_counts_every_attempt(client: MusicBrainzApiClient, server: _Server) -> None:
    """D9: a client counts each attempt, refused ones included; a view counts its own."""
    view = client.with_attempts(1)
    server.statuses = [503, 503, 503, 503]

    with pytest.raises(httpx.HTTPStatusError):
        _search(client, "Song 1")
    with pytest.raises(httpx.HTTPStatusError):
        _search(view, "Song 2")
    _search(view, "Song 3")

    assert client.requests_sent == 3
    assert view.requests_sent == 2


def test_fetchs_own_decorator_keeps_three_tries() -> None:
    """D10: ``_fetch``'s decorator is untouched; the fill's one attempt is a view's."""
    stop = MusicBrainzApiClient._fetch.retry.stop  # type: ignore[attr-defined]

    assert stop.max_attempt_number == 3


def test_only_the_fill_takes_a_one_attempt_view() -> None:
    """D10: no other caller (the artist stage, song matching, enrichment) takes a view."""
    callers = sorted(
        path.relative_to(BACKEND).as_posix()
        for path in BACKEND.rglob("*.py")
        if ".with_attempts(" in path.read_text(encoding="utf-8")
    )

    assert callers == ["tasks/cache_fill_tasks.py"]

"""Name words that Lucene would misread in the name + title search (phase 4.1 PR 1 review fix;
SPEC S2, AUD-R090).

``search_recordings_by_name_title`` requires every name word (``AND``). A word that reads as an
operator (AND, OR, NOT, in any case) would turn the query into a different one, and a word that
holds whitespace would split into terms that are not each required. Such a word is quoted as an
escaped phrase; every other word goes as it did before (the locked
``test_mb_client_fill_methods.py`` pins those).

Mock transport, the in-memory cache and no pacing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from urllib.parse import parse_qs

import httpx
import pytest

from backend.services.mb_client import MusicBrainzApiClient
from tests.fakes.musicbrainz_cache import FakeMusicBrainzCacheRepository

TITLE = "You Spin Me Round"
PHRASE = f'recording:"{TITLE}"'


class _Server:
    """Answers every request with no hits; records the queries."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.queries.append(parse_qs(request.url.query.decode())["query"][0])
        return httpx.Response(200, json={"count": 0, "offset": 0, "recordings": []})


@pytest.fixture
def cache() -> FakeMusicBrainzCacheRepository:
    return FakeMusicBrainzCacheRepository()


@pytest.fixture
def query_for(cache: FakeMusicBrainzCacheRepository) -> Iterator[Callable[[list[str]], str]]:
    """The query the client sends for *name_words* and ``TITLE``."""
    clients: list[MusicBrainzApiClient] = []

    def ask(name_words: list[str]) -> str:
        server = _Server()
        client = MusicBrainzApiClient(
            cache, transport=httpx.MockTransport(server), wait_turn=lambda: None
        )
        clients.append(client)
        client.search_recordings_by_name_title(name_words, TITLE)
        assert len(server.queries) == 1
        return server.queries[0]

    yield ask
    for client in clients:
        client.__exit__(None, None, None)


def test_an_operator_word_is_quoted(query_for: Callable[[list[str]], str]) -> None:
    """Dead or Alive: a bare OR would make the name match either side."""
    assert query_for(["DEAD", "OR", "ALIVE"]) == f'artist:(DEAD AND "OR" AND ALIVE) AND {PHRASE}'


@pytest.mark.parametrize(
    ("words", "artist"),
    [
        (["Tom", "and", "Jerry"], 'artist:(Tom AND "and" AND Jerry)'),
        (["Not", "Drowning", "Waving"], 'artist:("Not" AND Drowning AND Waving)'),
        (["Sly", "oR", "Robbie"], 'artist:(Sly AND "oR" AND Robbie)'),
        (["NOT"], 'artist:("NOT")'),
    ],
)
def test_operator_words_are_quoted_in_any_case(
    query_for: Callable[[list[str]], str], words: list[str], artist: str
) -> None:
    assert query_for(words) == f"{artist} AND {PHRASE}"


@pytest.mark.parametrize(
    ("words", "artist"),
    [
        (["Bon Jovi"], 'artist:("Bon Jovi")'),
        (["Earth", "Wind\tFire"], 'artist:(Earth AND "Wind\tFire")'),
        (["Brooks & Dunn"], r'artist:("Brooks \& Dunn")'),
        (['The "Boss" Band'], r'artist:("The \"Boss\" Band")'),
    ],
)
def test_a_word_with_whitespace_is_one_escaped_phrase(
    query_for: Callable[[list[str]], str], words: list[str], artist: str
) -> None:
    """A spaced word stays one required term; its specials are escaped inside the phrase."""
    assert query_for(words) == f"{artist} AND {PHRASE}"


def test_words_that_only_contain_operator_letters_stay_bare(
    query_for: Callable[[list[str]], str],
) -> None:
    assert query_for(["Orbital", "Andromeda", "Nothing", "Notorious"]) == (
        f"artist:(Orbital AND Andromeda AND Nothing AND Notorious) AND {PHRASE}"
    )


def test_quoting_leaves_the_cache_key_as_it_was(
    query_for: Callable[[list[str]], str], cache: FakeMusicBrainzCacheRepository
) -> None:
    """The key format is locked: lower-cased words joined by one space, then the title."""
    query_for(["DEAD", "OR", "ALIVE"])

    assert cache.get("recording-name-title:dead or alive:you spin me round") is not None

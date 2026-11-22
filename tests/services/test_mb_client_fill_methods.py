"""The client methods the cache fill adds (MusicBrainz cache fill, phase 4.1 PR 1; SPEC S2, E2,
E5; D3; AUD-R042, AUD-R090).

- ``search_recordings_by_name_title(name_words, title)``: one recording search that requires
  every word of the name and the title, cached under ``recording-name-title:{words}:{title}``
  (lower-cased, words joined by one space) in its own slim shape: the hit count, and each hit's
  id, score, title, length, disambiguation and full credit, joining words included
  (``_slim_recording`` keeps neither scores nor joining words). With no name words it searches
  the title alone, and the entry says ``title_only`` (E5). An empty result is cached (E2).
- ``search_artist_aliases_by_mbids(mbids)`` (D3): ``arid:`` artist searches, 100 MBIDs a request,
  each artist's aliases cached under ``artist-aliases:{mbid}``; an MBID the search index doesn't
  know is cached as unknown and never asked again.
- ``lookup_release_for_links(mbid)``: ``lookup_release`` with a remembered 404, cached under
  ``release-missing:{mbid}``, so the work-link tail never asks again (E2). ``lookup_release``
  itself still caches no 404: enrichment reads ``release:`` rows and must never find a marker.

Mock transport, the in-memory cache and no pacing; the recorded real responses are in
``test_mb_client_cassettes.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from backend.services.mb_client import MusicBrainzApiClient
from tests.fakes.musicbrainz_cache import FakeMusicBrainzCacheRepository

Handler = Callable[[httpx.Request], httpx.Response]

RECORDING = "9f0e2a5c-1b3d-4c5e-8f70-112233445566"
QUEEN = "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"
BOWIE = "5441c29d-3602-4898-b1a1-b77fa23b8e50"
RELEASE = "31de97e3-6c53-4ca6-a00d-152642eb7e4a"
GONE_RELEASE = "d0c2a6b4-3f1e-4b8a-9c7d-2e5f6a1b3c4d"


def _mbid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


class _Server:
    """Answers each request with *handler*; records them."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    def query(self, index: int = 0) -> str:
        return parse_qs(self.requests[index].url.query.decode())["query"][0]

    def param(self, name: str, index: int = 0) -> str:
        return parse_qs(self.requests[index].url.query.decode())[name][0]


@pytest.fixture
def cache() -> FakeMusicBrainzCacheRepository:
    return FakeMusicBrainzCacheRepository()


@pytest.fixture
def make_client(cache: FakeMusicBrainzCacheRepository) -> Iterator[Callable[[_Server], Any]]:
    clients: list[MusicBrainzApiClient] = []

    def factory(server: _Server) -> MusicBrainzApiClient:
        client = MusicBrainzApiClient(
            cache, transport=httpx.MockTransport(server), wait_turn=lambda: None
        )
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


def _entry(cache: FakeMusicBrainzCacheRepository, key: str) -> dict[str, Any]:
    found = cache.get(key)
    assert found is not None, f"no cache entry {key!r}"
    return found.response_data


# --- search_recordings_by_name_title ---------------------------------------------------------

UNDER_PRESSURE_HIT: dict[str, Any] = {
    "id": RECORDING,
    "score": 100,
    "title": "Under Pressure",
    "length": 248_000,
    "disambiguation": "single version",
    "video": False,
    "first-release-date": "1981-10-26",
    "artist-credit": [
        {
            "name": "Queen",
            "joinphrase": " & ",
            "artist": {"id": QUEEN, "name": "Queen", "sort-name": "Queen", "aliases": []},
        },
        {
            "name": "David Bowie",
            "joinphrase": "",
            "artist": {"id": BOWIE, "name": "David Bowie", "sort-name": "Bowie, David"},
        },
    ],
    "releases": [{"id": RELEASE, "title": "Hot Space", "status": "Official"}],
    "isrcs": ["GBUM71029604"],
    "tags": [{"name": "rock", "count": 3}],
}


def _recordings(hits: list[dict[str, Any]], count: int | None = None) -> Handler:
    def handle(_request: httpx.Request) -> httpx.Response:
        body = {"count": len(hits) if count is None else count, "offset": 0, "recordings": hits}
        return httpx.Response(200, json=body)

    return handle


def test_name_title_requires_every_word_and_the_title(make_client: Any) -> None:
    """Each name word is required (``AND``), Lucene-escaped, and the title is a phrase: the probe's
    lesson ("Under Pressure" by Queen & David Bowie)."""
    server = _Server(_recordings([UNDER_PRESSURE_HIT]))
    client = make_client(server)

    client.search_recordings_by_name_title(["Queen", "David", "Bowie"], "Under Pressure")

    assert len(server.requests) == 1
    assert server.requests[0].url.path == "/ws/2/recording/"
    assert server.query() == 'artist:(Queen AND David AND Bowie) AND recording:"Under Pressure"'
    assert server.param("limit") == "25"
    assert server.param("fmt") == "json"


def test_name_title_escapes_lucene_specials(make_client: Any) -> None:
    server = _Server(_recordings([]))
    client = make_client(server)

    client.search_recordings_by_name_title(["AC/DC", "(live)"], 'Say "Hello"')

    assert server.query() == r'artist:(AC\/DC AND \(live\)) AND recording:"Say \"Hello\""'


def test_name_title_keeps_count_scores_and_full_credits(
    make_client: Any, cache: FakeMusicBrainzCacheRepository
) -> None:
    """The entry keeps what 4.2 and 4.4 read: the hit count, and per hit its score and its whole
    credit with the joining words; the releases, ISRCs and tags are dropped."""
    server = _Server(_recordings([UNDER_PRESSURE_HIT], count=7))
    client = make_client(server)

    result = client.search_recordings_by_name_title(["queen", "david", "bowie"], "under pressure")

    expected = {
        "count": 7,
        "title_only": False,
        "recordings": [
            {
                "id": RECORDING,
                "score": 100,
                "title": "Under Pressure",
                "length": 248_000,
                "disambiguation": "single version",
                "artist-credit": [
                    {
                        "name": "Queen",
                        "joinphrase": " & ",
                        "artist": {"id": QUEEN, "name": "Queen"},
                    },
                    {
                        "name": "David Bowie",
                        "joinphrase": "",
                        "artist": {"id": BOWIE, "name": "David Bowie"},
                    },
                ],
            }
        ],
    }
    assert result == expected
    key = "recording-name-title:queen david bowie:under pressure"
    assert _entry(cache, key) == expected
    stored = cache.get(key)
    assert stored is not None and stored.entity_type == "recording-name-title"


def test_name_title_leaves_out_what_a_hit_lacks(make_client: Any) -> None:
    """No length and no disambiguation on the hit: neither key in the entry; a credit with no
    joining words gets ``""``."""
    bare = {
        "id": RECORDING,
        "score": 88,
        "title": "Smoke From a Distant Fire",
        "disambiguation": "",
        "artist-credit": [{"name": "Sanford & Townsend", "artist": {"id": QUEEN, "name": "x"}}],
    }
    server = _Server(_recordings([bare]))
    client = make_client(server)

    [hit] = client.search_recordings_by_name_title(["sanford"], "smoke")["recordings"]

    assert set(hit) == {"id", "score", "title", "artist-credit"}
    assert hit["artist-credit"] == [
        {"name": "Sanford & Townsend", "joinphrase": "", "artist": {"id": QUEEN, "name": "x"}}
    ]


def test_name_title_is_cached_by_lowercased_words_and_title(make_client: Any) -> None:
    """A second ask, in any case, is answered from the cache: no request."""
    server = _Server(_recordings([UNDER_PRESSURE_HIT]))
    client = make_client(server)
    first = client.search_recordings_by_name_title(["Queen", "Bowie"], "Under Pressure")

    again = client.search_recordings_by_name_title(["QUEEN", "BOWIE"], "UNDER PRESSURE")

    assert again == first
    assert len(server.requests) == 1
    assert (client.live_fetches, client.cache_hits) == (1, 1)


def test_name_title_caches_an_empty_result(
    make_client: Any, cache: FakeMusicBrainzCacheRepository
) -> None:
    """E2: nothing found is cached too, so the query is never repeated."""
    server = _Server(_recordings([]))
    client = make_client(server)

    first = client.search_recordings_by_name_title(["zzyzx"], "nothing at all")
    again = client.search_recordings_by_name_title(["zzyzx"], "nothing at all")

    assert first == again == {"count": 0, "title_only": False, "recordings": []}
    assert len(server.requests) == 1
    assert _entry(cache, "recording-name-title:zzyzx:nothing at all")["count"] == 0


def test_no_name_words_searches_the_title_alone(
    make_client: Any, cache: FakeMusicBrainzCacheRepository
) -> None:
    """E5: a name of only initials or stop words is searched by its title, and the entry says so,
    so 4.2 never takes it for a name signal."""
    server = _Server(_recordings([UNDER_PRESSURE_HIT]))
    client = make_client(server)

    result = client.search_recordings_by_name_title([], "Under Pressure")

    assert server.query() == 'recording:"Under Pressure"'
    assert result["title_only"] is True
    assert _entry(cache, "recording-name-title::under pressure")["title_only"] is True


def test_name_title_raises_a_client_error(make_client: Any) -> None:
    """A permanent error (400) is the caller's: nothing is cached."""
    server = _Server(lambda _r: httpx.Response(400, json={"error": "bad query"}))
    client = make_client(server)

    with pytest.raises(httpx.HTTPStatusError):
        client.search_recordings_by_name_title(["x"], "y")

    again = _Server(_recordings([]))
    retry = make_client(again)
    retry.search_recordings_by_name_title(["x"], "y")
    assert len(again.requests) == 1


# --- search_artist_aliases_by_mbids ----------------------------------------------------------


def _alias(name: str, **extra: Any) -> dict[str, Any]:
    alias = {
        "name": name,
        "sort-name": name,
        "type": "Artist name",
        "type-id": "894afba6-2816-3c24-8072-eadb66bd04bc",
        "locale": None,
        "primary": None,
        "begin-date": None,
        "end-date": None,
        "ended": False,
    }
    alias.update(extra)
    return alias


class _AliasServer:
    """Answers ``arid:(a OR b ...)`` artist searches from *known* (MBID -> aliases)."""

    def __init__(self, known: dict[str, list[dict[str, Any]]]) -> None:
        self.known = known
        self.server = _Server(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())["query"][0]
        asked = query[len("arid:(") : -1].split(" OR ")
        hits = [
            {
                "id": m,
                "name": f"Artist {m[:8]}",
                "sort-name": f"Artist {m[:8]}",
                "score": 100,
                "tags": [{"name": "rock", "count": 1}],
                "life-span": {"ended": None},
                **({"aliases": self.known[m]} if self.known[m] else {}),
            }
            for m in asked
            if m in self.known
        ]
        return httpx.Response(200, json={"count": len(hits), "artists": hits})

    def asked(self, index: int) -> list[str]:
        return self.server.query(index)[len("arid:(") : -1].split(" OR ")


def test_aliases_one_request_per_hundred_artists(make_client: Any) -> None:
    mbids = [_mbid(n) for n in range(150)]
    aliases = _AliasServer({m: [_alias(f"A {m[:8]}")] for m in mbids})
    client = make_client(aliases.server)

    found = client.search_artist_aliases_by_mbids(mbids)

    assert len(aliases.server.requests) == 2
    assert aliases.server.requests[0].url.path == "/ws/2/artist/"
    assert aliases.asked(0) == mbids[:100]
    assert aliases.asked(1) == mbids[100:]
    assert aliases.server.param("limit") == "100"
    assert set(found) == set(mbids)


def test_aliases_keep_their_names_and_kinds(
    make_client: Any, cache: FakeMusicBrainzCacheRepository
) -> None:
    """Each alias keeps its name, sort name, type, locale and primary flag; the artist's other
    fields (tags, life span, score) are not kept."""
    mellencamp = _mbid(1)
    aliases = _AliasServer(
        {
            mellencamp: [
                _alias("John Cougar", type="Artist name"),
                _alias("Johnny Cougar", **{"sort-name": "Cougar, Johnny"}, type="Search hint"),
                _alias("ジョン・メレンキャンプ", locale="ja", primary=True),
            ]
        }
    )
    client = make_client(aliases.server)

    found = client.search_artist_aliases_by_mbids([mellencamp])

    expected = [
        {
            "name": "John Cougar",
            "sort-name": "John Cougar",
            "type": "Artist name",
            "locale": None,
            "primary": None,
        },
        {
            "name": "Johnny Cougar",
            "sort-name": "Cougar, Johnny",
            "type": "Search hint",
            "locale": None,
            "primary": None,
        },
        {
            "name": "ジョン・メレンキャンプ",
            "sort-name": "ジョン・メレンキャンプ",
            "type": "Artist name",
            "locale": "ja",
            "primary": True,
        },
    ]
    assert found == {mellencamp: expected}
    stored = cache.get(f"artist-aliases:{mellencamp}")
    assert stored is not None
    assert stored.entity_type == "artist-aliases"
    assert stored.entity_mbid == mellencamp
    assert stored.response_data == {"id": mellencamp, "aliases": expected}


def test_an_artist_with_no_aliases_is_found_with_none(
    make_client: Any, cache: FakeMusicBrainzCacheRepository
) -> None:
    plain = _mbid(2)
    aliases = _AliasServer({plain: []})
    client = make_client(aliases.server)

    assert client.search_artist_aliases_by_mbids([plain]) == {plain: []}
    assert _entry(cache, f"artist-aliases:{plain}") == {"id": plain, "aliases": []}


def test_an_unknown_artist_is_remembered_and_not_asked_again(make_client: Any) -> None:
    known, unknown = _mbid(3), _mbid(4)
    aliases = _AliasServer({known: [_alias("K")]})
    client = make_client(aliases.server)

    first = client.search_artist_aliases_by_mbids([known, unknown])
    again = client.search_artist_aliases_by_mbids([known, unknown])

    assert first == again
    assert set(first) == {known}
    assert [a["name"] for a in first[known]] == ["K"]
    assert len(aliases.server.requests) == 1
    assert client.cache_hits == 2


def test_only_the_artists_not_cached_are_asked(make_client: Any) -> None:
    cached, fresh = _mbid(5), _mbid(6)
    aliases = _AliasServer({cached: [_alias("C")], fresh: [_alias("F")]})
    client = make_client(aliases.server)
    client.search_artist_aliases_by_mbids([cached])

    found = client.search_artist_aliases_by_mbids([cached, fresh])

    assert aliases.asked(1) == [fresh]
    assert [a["name"] for a in found[cached]] == ["C"]
    assert [a["name"] for a in found[fresh]] == ["F"]


def test_aliases_skip_malformed_ids_and_ask_nothing_for_none(make_client: Any) -> None:
    aliases = _AliasServer({})
    client = make_client(aliases.server)

    assert client.search_artist_aliases_by_mbids(["not-a-uuid"]) == {}
    assert client.search_artist_aliases_by_mbids([]) == {}
    assert aliases.server.requests == []


# --- lookup_release_for_links ----------------------------------------------------------------

RELEASE_BODY: dict[str, Any] = {
    "id": RELEASE,
    "title": "Master of Puppets",
    "media": [
        {
            "tracks": [
                {
                    "recording": {
                        "id": RECORDING,
                        "title": "Battery",
                        "relations": [
                            {"type": "performance", "work": {"id": _mbid(9), "title": "Battery"}}
                        ],
                    }
                }
            ]
        }
    ],
}


def _releases(known: dict[str, dict[str, Any]]) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        mbid = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        if mbid in known:
            return httpx.Response(200, json=known[mbid])
        return httpx.Response(404, json={"error": "Not Found"})

    return handle


def test_a_release_is_looked_up_and_cached_as_lookup_release_caches_it(
    make_client: Any, cache: FakeMusicBrainzCacheRepository
) -> None:
    server = _Server(_releases({RELEASE: RELEASE_BODY}))
    client = make_client(server)

    release = client.lookup_release_for_links(RELEASE)

    assert release == RELEASE_BODY
    assert "work-rels" in server.param("inc")
    assert _entry(cache, f"release:{RELEASE}") == RELEASE_BODY
    assert cache.get(f"release-missing:{RELEASE}") is None


def test_release_404_remembered(make_client: Any, cache: FakeMusicBrainzCacheRepository) -> None:
    """E2: a release MusicBrainz no longer has is remembered under ``release-missing:``, and the
    next ask answers None with no request."""
    server = _Server(_releases({}))
    client = make_client(server)

    assert client.lookup_release_for_links(GONE_RELEASE) is None
    assert client.lookup_release_for_links(GONE_RELEASE) is None

    assert len(server.requests) == 1
    stored = cache.get(f"release-missing:{GONE_RELEASE}")
    assert stored is not None and stored.entity_mbid == GONE_RELEASE
    assert cache.get(f"release:{GONE_RELEASE}") is None


def test_lookup_release_still_remembers_no_404(
    make_client: Any, cache: FakeMusicBrainzCacheRepository
) -> None:
    """Enrichment's path is unchanged: ``lookup_release`` caches nothing for a 404, and a marker
    the fill wrote never answers it."""
    server = _Server(_releases({}))
    client = make_client(server)

    assert client.lookup_release(GONE_RELEASE) is None
    assert cache.get(f"release-missing:{GONE_RELEASE}") is None
    client.lookup_release_for_links(GONE_RELEASE)
    assert client.lookup_release(GONE_RELEASE) is None

    assert len(server.requests) == 3


def test_a_cached_release_answers_before_the_marker_is_read(
    make_client: Any, cache: FakeMusicBrainzCacheRepository
) -> None:
    server = _Server(_releases({RELEASE: RELEASE_BODY}))
    client = make_client(server)
    client.lookup_release(RELEASE)

    assert client.lookup_release_for_links(RELEASE) == RELEASE_BODY
    assert len(server.requests) == 1


def test_a_transient_release_error_is_not_remembered(make_client: Any) -> None:
    """Only a 404 is remembered: a 400 (a malformed id) raises as ``lookup_release`` does, and
    nothing is cached, so a later ask goes out again."""
    server = _Server(lambda _r: httpx.Response(400, json={"error": "Invalid mbid."}))
    client = make_client(server)

    with pytest.raises(httpx.HTTPStatusError):
        client.lookup_release_for_links(RELEASE)
    with pytest.raises(httpx.HTTPStatusError):
        client.lookup_release_for_links(RELEASE)

    assert len(server.requests) == 2

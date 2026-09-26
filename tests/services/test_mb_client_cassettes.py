"""Recorded-response (VCR) tests for MusicBrainzApiClient.

Every other MusicBrainz test uses hand-written replies (FakeMbClient) that
encode what we *believe* the API returns. These tests replay real MusicBrainz
responses saved under ``cassettes/test_mb_client_cassettes/`` and check that
the fields our code reads are really there, with the types it expects.

Replay is offline and needs no database. A missing cassette fails the test
instead of reaching the network (pytest-recording defaults to
``--record-mode=none``).

Re-record after changing a request, or periodically to catch API drift.
Run single-process so the 1 req/s rate limiter is honoured:

    uv run pytest tests/services/test_mb_client_cassettes.py -n 0 --record-mode=rewrite
"""
from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest

from backend.services.library_enrichment_service import (
    _extract_artist_from_credits,
    _extract_work_from_relations,
)
from backend.services.mb_client import MusicBrainzApiClient
from tests.fakes.musicbrainz_cache import FakeMusicBrainzCacheRepository

METALLICA_MBID = "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"
# "Enter Sandman" studio recording — carries a `performance` work relation.
ENTER_SANDMAN_RECORDING_MBID = "3edac058-cf57-4175-8650-a74ca61652a4"
# "Master of Puppets", 1986 US CD release (8 tracks).
MASTER_OF_PUPPETS_RELEASE_MBID = "31de97e3-6c53-4ca6-a00d-152642eb7e4a"
# Well-formed UUID that matches no MusicBrainz entity.
UNKNOWN_MBID = "d0c2a6b4-3f1e-4b8a-9c7d-2e5f6a1b3c4d"

pytestmark = pytest.mark.vcr


@pytest.fixture(scope="module")
def vcr_config() -> dict[str, object]:
    # Store readable JSON bodies rather than gzip bytes.
    return {"decode_compressed_response": True}


@pytest.fixture(autouse=True)
def _skip_rate_limit_on_replay(record_mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replays never touch MusicBrainz, so the 1.1s per-call throttle is dead time."""
    if record_mode == "none":
        monkeypatch.setattr("backend.services.mb_client._rate_limit", lambda: None)


@pytest.fixture
def client() -> Iterator[MusicBrainzApiClient]:
    with MusicBrainzApiClient(FakeMusicBrainzCacheRepository()) as mb_client:
        yield mb_client


# --- search_artist -----------------------------------------------------------


def test_search_artist_returns_scored_candidates(client: MusicBrainzApiClient) -> None:
    results = client.search_artist("Metallica")

    assert len(results) > 1
    best = results[0]
    assert best["id"] == METALLICA_MBID
    assert best["name"] == "Metallica"
    assert best["sort-name"] == "Metallica"
    # Artist matching does arithmetic on `score` (gap between top two), so it
    # must arrive as an int, not a numeric string.
    assert all(isinstance(r["score"], int) for r in results)
    assert best["score"] == 100
    assert results[1]["score"] < best["score"]


def test_search_artist_accepts_slash_in_name(client: MusicBrainzApiClient) -> None:
    # search_artist sends the name unescaped; "/" is a Lucene special character.
    results = client.search_artist("AC/DC")

    assert results[0]["name"] == "AC/DC"
    assert results[0]["id"] == "66c662b6-6e2f-4930-8610-912e24c63ed1"


def test_search_artist_matches_accented_name(client: MusicBrainzApiClient) -> None:
    results = client.search_artist("Beyoncé")

    assert results[0]["name"] == "Beyoncé"
    assert results[0]["id"] == "859d0860-d480-4efd-970c-c05d5f1776b8"


def test_search_artist_returns_typographic_apostrophe(client: MusicBrainzApiClient) -> None:
    # We search with a straight apostrophe; MusicBrainz stores the canonical
    # name with U+2019. Anything comparing names verbatim must normalise first.
    results = client.search_artist("Guns N' Roses")

    assert results[0]["id"] == "eeb1195b-f213-4ce1-b28c-8565211f8e43"
    assert results[0]["name"] == "Guns N’ Roses"


def test_search_artist_returns_empty_list_when_nothing_matches(
    client: MusicBrainzApiClient,
) -> None:
    assert client.search_artist("qxzvjwkq plomfrth") == []


# --- search_recording --------------------------------------------------------


def test_search_recording_scopes_results_to_artist(client: MusicBrainzApiClient) -> None:
    results = client.search_recording(METALLICA_MBID, "Enter Sandman", limit=5)

    assert len(results) == 5
    for recording in results:
        assert recording["title"] == "Enter Sandman"
        # A recording with no known duration omits "length" entirely (seen
        # 2026-09-26 on a live Seoul 1998 recording); readers use .get().
        assert isinstance(recording.get("length", 0), int)
        credit = _extract_artist_from_credits(recording["artist-credit"])
        assert credit is not None
        assert credit[0] == METALLICA_MBID


# --- lookup_recording --------------------------------------------------------


def test_lookup_recording_performance_relation_yields_work(
    client: MusicBrainzApiClient,
) -> None:
    recording = client.lookup_recording(ENTER_SANDMAN_RECORDING_MBID)

    assert recording is not None
    assert recording["title"] == "Enter Sandman"
    assert isinstance(recording["length"], int)
    assert _extract_artist_from_credits(recording["artist-credit"]) == (
        METALLICA_MBID, "Metallica", "Metallica",
    )
    work = _extract_work_from_relations(recording["relations"])
    assert work is not None
    assert work[1] == "Enter Sandman"


# --- lookup_release ----------------------------------------------------------


def test_lookup_release_nests_recordings_under_media_tracks(
    client: MusicBrainzApiClient,
) -> None:
    release = client.lookup_release(MASTER_OF_PUPPETS_RELEASE_MBID)

    assert release is not None
    assert release["title"] == "Master of Puppets"
    credit = _extract_artist_from_credits(release["artist-credit"])
    assert credit is not None
    assert credit[0] == METALLICA_MBID

    tracks = [track for medium in release["media"] for track in medium["tracks"]]
    assert len(tracks) == 8
    for track in tracks:
        recording = track["recording"]
        assert recording["id"]
        assert recording["title"]
        assert isinstance(recording["length"], int)


def test_lookup_release_track_performance_relation_yields_work(
    client: MusicBrainzApiClient,
) -> None:
    """A release lookup carries each recording's work relation.

    ``enrich_by_release`` reads ``relations`` off every track's recording; the
    request must ask for ``recording-level-rels+work-rels`` or MusicBrainz
    returns the recordings with no relations at all and no work is ever
    linked from the release path.
    """
    release = client.lookup_release(MASTER_OF_PUPPETS_RELEASE_MBID)

    assert release is not None
    tracks = [track for medium in release["media"] for track in medium["tracks"]]
    works = [_extract_work_from_relations(track["recording"]["relations"]) for track in tracks]
    assert all(work is not None for work in works)
    assert [work[1] for work in works if work is not None][:2] == [
        "Battery", "Master of Puppets",
    ]


# --- lookup_artist -----------------------------------------------------------


def test_lookup_artist_includes_aliases_and_tags(client: MusicBrainzApiClient) -> None:
    artist = client.lookup_artist(METALLICA_MBID)

    assert artist is not None
    assert artist["name"] == "Metallica"
    assert artist["sort-name"] == "Metallica"
    assert artist["aliases"]
    assert artist["tags"]
    assert all(isinstance(tag["count"], int) for tag in artist["tags"])
    # MbAliasEntry types `primary` as bool, but MusicBrainz sends null for
    # non-primary aliases. Treat it as bool | None when reading.
    assert {alias["primary"] for alias in artist["aliases"]} <= {True, False, None}
    assert any(alias["primary"] is None for alias in artist["aliases"])


# --- not-found and invalid IDs -----------------------------------------------

_LOOKUPS: dict[str, Callable[[MusicBrainzApiClient, str], object]] = {
    "artist": MusicBrainzApiClient.lookup_artist,
    "recording": MusicBrainzApiClient.lookup_recording,
    "release": MusicBrainzApiClient.lookup_release,
}


@pytest.mark.parametrize("entity", list(_LOOKUPS))
def test_lookup_unknown_mbid_returns_none(client: MusicBrainzApiClient, entity: str) -> None:
    assert _LOOKUPS[entity](client, UNKNOWN_MBID) is None


@pytest.mark.parametrize("entity", list(_LOOKUPS))
def test_lookup_malformed_mbid_raises_bad_request(
    client: MusicBrainzApiClient, entity: str
) -> None:
    # MusicBrainz answers a malformed ID (e.g. a corrupt file tag) with 400,
    # not 404, so the client raises instead of returning None.
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        _LOOKUPS[entity](client, "not-a-uuid")

    assert exc_info.value.response.status_code == 400


# --- search_recordings_by_mbids ------------------------------------------------

# "Late Bloom" (Amy Ray) — on the 2001 release "Stag".
LATE_BLOOM_RECORDING_MBID = "001f323a-7361-4b74-a3df-8cc0f87551e7"
STAG_RELEASE_MBID = "a8718a01-aacd-44f7-aaf8-68e04d0b2eb7"
# A recording MBID MusicBrainz has merged away: a direct lookup answers 301.
MERGED_RECORDING_MBID = "01ec25c7-5684-44e5-8fed-5c48217c3baf"


def test_search_recordings_by_mbids_carries_enrichment_fields(
    client: MusicBrainzApiClient,
) -> None:
    found = client.search_recordings_by_mbids([
        ENTER_SANDMAN_RECORDING_MBID, LATE_BLOOM_RECORDING_MBID, MERGED_RECORDING_MBID,
    ])

    # Merged MBIDs are not in the search index; the caller falls back for them.
    assert set(found) == {ENTER_SANDMAN_RECORDING_MBID, LATE_BLOOM_RECORDING_MBID}
    late_bloom = found[LATE_BLOOM_RECORDING_MBID]
    assert late_bloom["title"] == "Late Bloom"
    assert isinstance(late_bloom["length"], int)
    assert _extract_artist_from_credits(late_bloom["artist-credit"]) == (
        "511c533d-d5f5-4b00-8bc8-b45344fca524", "Amy Ray", "Ray, Amy",
    )
    assert STAG_RELEASE_MBID in {r["id"] for r in late_bloom["releases"]}
    # Search results carry no relations: works still need lookup_recording.
    assert "relations" not in late_bloom
    assert client.live_fetches == 1

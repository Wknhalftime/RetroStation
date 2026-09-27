"""Syrupy characterisation tests for ``_enhance_artist`` (AUD-R008 gate 1).

Locks the CURRENT observable behaviour of the artist-enhancement tiers
before Tier 1 (the ``artist.mbid is None`` branch — MB name search that
auto-links an MBID onto a bare local artist) is deleted per AUD-R008.

Every snapshot here must stay byte-identical across the AUD-R008 change
EXCEPT the ``test_tier1_*`` cases: the ruling deliberately replaces the
auto-link behaviour with an immediate FAILED/quarantine outcome (an
MBID-less artist reaching ``_enhance_artist`` is a logic bug once artists
can only gain an MBID via release/recording enrichment). Those snapshots
are expected to change in the behaviour-changing commit, and only those.

No Postgres, no network: `conn` and `repos` are lightweight recorders, and
`FakeMbClient` returns canned payloads shaped like the real
`MusicBrainzApiClient` / cassette responses (id, score, sort-name,
disambiguation for search hits; id, name, sort-name, disambiguation for
artist lookups).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from syrupy.assertion import SnapshotAssertion

from backend.domain.catalog import Artist, CatalogSource
from backend.tasks.mb_enrichment_tasks import ArtistEnhanceOutcome, _enhance_artist
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.mb_client import FakeMbClient


class _RecordingConn:
    """Fake psycopg connection: records (sql_text, params) for every execute().

    `_enhance_artist` never reads back a row from `conn` — every write goes
    through `_apply_artist_updates`, a single positional-args `execute()`
    call — so a bare recorder is sufficient; no query results are ever
    consumed.
    """

    def __init__(self) -> None:
        self.executed: list[dict[str, Any]] = []

    def execute(self, query: Any, params: tuple[Any, ...] = ()) -> None:
        text = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.executed.append({"sql": text, "params": list(params)})


@dataclass
class _ReposStub:
    """Minimal stand-in for RepositoryFactory.

    `_enhance_artist` only ever reads `repos.artists`, so a stub exposing
    just that attribute is enough — mirrors the existing convention in
    tests/tasks/test_mb_enrichment_artist_tiers.py, which passes a bare
    MagicMock() for the same reason.
    """

    artists: FakeArtistRepository = field(default_factory=FakeArtistRepository)


def _bare_artist(**overrides: Any) -> Artist:
    base: dict[str, Any] = dict(
        id="local-uuid-1",
        name="Unknown Band",
        sort_name="Unknown Band",
        disambiguation=None,
        needs_enhancement=True,
        enhanced_at=None,
        enhancement_error=None,
        mbid=None,
        origin=CatalogSource.LOCAL,
        normalized_name="unknown band",
    )
    base.update(overrides)
    return Artist(**base)


def _mbid_artist(**overrides: Any) -> Artist:
    base: dict[str, Any] = dict(
        id="local-uuid-2",
        name="Known Band",
        sort_name="Known Band",  # == name -> still counts as an unfilled default
        disambiguation=None,
        needs_enhancement=True,
        enhanced_at=None,
        enhancement_error=None,
        mbid="mb-uuid-known",
        origin=CatalogSource.MUSICBRAINZ,
        normalized_name="known band",
    )
    base.update(overrides)
    return Artist(**base)


def _run(
    artist: Artist,
    mb_client: FakeMbClient,
    *,
    mbid_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run `_enhance_artist` and capture every observable side effect."""
    conn = _RecordingConn()
    repos = _ReposStub()
    repos.artists.upsert(artist)

    outcome: ArtistEnhanceOutcome = _enhance_artist(
        artist,
        mb_client,
        conn,  # type: ignore[arg-type]
        repos,  # type: ignore[arg-type]
        mbid_map=mbid_map,
    )

    stored = repos.artists.get_by_id(artist.id)
    assert stored is not None
    return {
        "outcome": outcome.value,
        "sql_executed": conn.executed,
        "artist_after": {
            "needs_enhancement": stored.needs_enhancement,
            "enhancement_error": stored.enhancement_error,
            "mbid": stored.mbid,
            "sort_name": stored.sort_name,
            "disambiguation": stored.disambiguation,
        },
    }


# ---------------------------------------------------------------------------
# Tier 1 (today's behaviour): artist.mbid is None. AUD-R008 deletes this
# entire branch; these three cases are the ones expected to change.
# ---------------------------------------------------------------------------


def test_tier1_high_confidence_auto_links_mbid(snapshot: SnapshotAssertion) -> None:
    artist = _bare_artist()
    fake_client = FakeMbClient(
        responses={
            "Unknown Band": [
                {
                    "id": "mb-uuid-123",
                    "score": 99,
                    "sort-name": "Band, Unknown",
                    "disambiguation": "British rock band",
                }
            ]
        }
    )

    assert _run(artist, fake_client) == snapshot


def test_tier1_low_confidence_marks_enhanced_without_mbid(snapshot: SnapshotAssertion) -> None:
    artist = _bare_artist()
    fake_client = FakeMbClient(responses={"Unknown Band": [{"id": "mb-uuid-X", "score": 40}]})

    assert _run(artist, fake_client) == snapshot


def test_tier1_no_search_results_marks_enhanced(snapshot: SnapshotAssertion) -> None:
    artist = _bare_artist()
    fake_client = FakeMbClient(responses={})

    assert _run(artist, fake_client) == snapshot


# ---------------------------------------------------------------------------
# Tier 2 (MBID known, fields filled from a lookup). Must be byte-identical
# after AUD-R008 — Tier 2/3 behaviour is kept verbatim.
# ---------------------------------------------------------------------------


def test_tier2_fills_disambiguation_and_sort_name(snapshot: SnapshotAssertion) -> None:
    artist = _mbid_artist()
    fake_client = FakeMbClient(
        artists={
            "mb-uuid-known": {
                "id": "mb-uuid-known",
                "name": "Known Band",
                "sort-name": "Band, Known",
                "disambiguation": "US indie rock band",
            }
        }
    )

    assert _run(artist, fake_client) == snapshot


# ---------------------------------------------------------------------------
# Tier 3 (nothing to change). Must be byte-identical after AUD-R008.
# ---------------------------------------------------------------------------


def test_tier3_all_fields_present_no_update(snapshot: SnapshotAssertion) -> None:
    artist = _mbid_artist(disambiguation="Already set", sort_name="Band, Known")
    fake_client = FakeMbClient(
        artists={
            "mb-uuid-known": {
                "id": "mb-uuid-known",
                "name": "Known Band",
                "sort-name": "Band, Known",
                "disambiguation": "Already set",
            }
        }
    )

    assert _run(artist, fake_client) == snapshot


# ---------------------------------------------------------------------------
# A 404 lookup on an MBID-known artist becomes FAILED. Must be
# byte-identical after AUD-R008.
# ---------------------------------------------------------------------------


def test_tier2_404_lookup_marks_enhancement_failed(snapshot: SnapshotAssertion) -> None:
    artist = _mbid_artist()
    fake_client = FakeMbClient(artists={})  # lookup_artist returns None

    assert _run(artist, fake_client) == snapshot


# ---------------------------------------------------------------------------
# The mbid_map pre-fetched path (both a hit with data and a cached-404
# hit). Must be byte-identical after AUD-R008.
# ---------------------------------------------------------------------------


def test_mbid_map_hit_short_circuits_live_lookup(snapshot: SnapshotAssertion) -> None:
    artist = _mbid_artist()
    fake_client = FakeMbClient()  # no `artists` entries: a live lookup would 404
    mbid_map = {
        "mb-uuid-known": {
            "id": "mb-uuid-known",
            "name": "Known Band",
            "sort-name": "Band, Known",
            "disambiguation": "From map",
        }
    }

    result = _run(artist, fake_client, mbid_map=mbid_map)
    result["live_lookup_calls"] = [c for c in fake_client.calls if c.startswith("lookup_artist:")]
    assert result == snapshot


def test_mbid_map_cached_404_marks_enhancement_failed(snapshot: SnapshotAssertion) -> None:
    artist = _mbid_artist()
    fake_client = FakeMbClient()
    mbid_map: dict[str, Any] = {"mb-uuid-known": None}

    result = _run(artist, fake_client, mbid_map=mbid_map)
    result["live_lookup_calls"] = [c for c in fake_client.calls if c.startswith("lookup_artist:")]
    assert result == snapshot

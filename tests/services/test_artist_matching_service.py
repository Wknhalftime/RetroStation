"""Orchestrator-level tests for `match_artists_for_playlist`.

# PR 4 replaced _try_rule_match / _try_exact_match / _try_fuzzy_match /
# _try_mb_match with the ArtistMatchingEngine Strategy Pattern. Tests that
# pinned the legacy function signatures were either deleted (impl-detail
# tests) or rewritten to assert the same external behavior against the new
# strategies or the rewritten service function. Reason-string baseline
# tests were updated in-place from "no reason persisted" to "ReasonCode
# populated" — documented behavior change, not a regression.

Individual strategy behaviors are covered in test_artist_matching_strategies.py
and the engine's dispatch is covered in test_artist_matching_engine.py. This
file exercises the wiring: strategy order, persistence, no-match fallback,
and the AUTO_REJECTED cascade preserved from the legacy implementation.
"""
from __future__ import annotations

from uuid import uuid4

import httpx

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.catalog import Artist
from backend.domain.enums import MatchStatus, MatchTier, TargetType
from backend.domain.matching import MappingRule
from backend.services.artist_matching_service import match_artists_for_playlist
from backend.services.matching_constants import MB_SCORE_GAP
from backend.services.matching_reasons import ReasonCode
from backend.services.normalization import normalize_artist
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient


def _pending_artist(
    name: str,
    broadcast_artist_repo: FakeBroadcastArtistRepository,
    playlist_id: object,
) -> BroadcastArtist:
    artist = BroadcastArtist(
        id=uuid4(),
        original_name=name,
        normalized_name=normalize_artist(name),
    )
    broadcast_artist_repo.upsert(artist)
    broadcast_artist_repo.register_playlist_artist(playlist_id, artist.id)  # type: ignore[arg-type]
    return artist


# ---------------------------------------------------------------------------
# Replacement orchestrator-level coverage for deleted _try_* characterization
# ---------------------------------------------------------------------------


def test_match_artists_rule_hit_creates_match_with_manual_tier() -> None:
    """Covers old _try_rule_match: a mapping rule hit writes AUTO_MATCHED +
    MANUAL-tier Match row."""
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    match_repo = FakeMatchRepository()
    rules_repo = FakeMappingRuleRepository()

    artist = _pending_artist("AC/DC", broadcast_artist_repo, playlist_id)
    rules_repo.create(MappingRule(
        id=uuid4(),
        source_pattern=artist.normalized_name,
        target_type=TargetType.ARTIST,
        target_id="mbid-acdc",
        priority=10,
    ))

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=FakeBroadcastTrackIdentityRepository(),
        artist_repo=FakeArtistRepository(),
        match_repo=match_repo,
        rules_repo=rules_repo,
        mb_client=FakeMbClient(),
    )

    stored = broadcast_artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    created = match_repo.get_by_artist(artist.id)
    assert created is not None
    assert created.target_id == "mbid-acdc"
    assert created.target_type == TargetType.ARTIST
    assert created.confidence_score == 100.0
    assert created.match_tier == MatchTier.MANUAL


def test_match_artists_exact_match_creates_match_with_normalization_tier() -> None:
    """Covers old _try_exact_match: exact normalized-name hit against the
    local canonical catalog writes AUTO_MATCHED + NORMALIZATION-tier Match."""
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    # mbid must be populated: NormalizationStrategy filters out mbid=None
    # canonicals since Match.target_id is consumed as an MBID downstream.
    artist_repo.upsert(Artist(
        id="mbid-metallica",
        name="Metallica",
        sort_name="Metallica",
        mbid="mbid-metallica",
    ))
    artist = _pending_artist("METALLICA", broadcast_artist_repo, playlist_id)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=FakeBroadcastTrackIdentityRepository(),
        artist_repo=artist_repo,
        match_repo=match_repo,
        rules_repo=FakeMappingRuleRepository(),
        mb_client=FakeMbClient(),
    )

    stored = broadcast_artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    created = match_repo.get_by_artist(artist.id)
    assert created is not None
    assert created.target_id == "mbid-metallica"
    assert created.match_tier == MatchTier.NORMALIZATION
    assert created.confidence_score == 100.0


def test_match_artists_fuzzy_mid_persists_low_confidence_reason() -> None:
    """Updated reason-string baseline: a mid-confidence fuzzy hit now
    persists ReasonCode.LOW_CONFIDENCE + a formatted detail string.

    Previously (PR 2 baseline) this was pinned to `_reason_codes.get(...) is
    None` because the legacy fuzzy path never passed reason kwargs. PR 4
    intentionally surfaces the reason through the strategy result.
    """
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    artist_repo.upsert(Artist(
        id="mbid-metallica",
        name="Metallica",
        sort_name="Metallica",
        mbid="mbid-metallica",
    ))
    artist = _pending_artist("Metalikka", broadcast_artist_repo, playlist_id)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=FakeBroadcastTrackIdentityRepository(),
        artist_repo=artist_repo,
        match_repo=match_repo,
        rules_repo=FakeMappingRuleRepository(),
        mb_client=FakeMbClient(),
        mb_score_gap=MB_SCORE_GAP,
    )

    stored = broadcast_artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    # No Match row on NEEDS_REVIEW (service only writes Match on AUTO_MATCHED).
    assert match_repo.get_by_artist(artist.id) is None
    # NEW behavior: reason is persisted.
    assert broadcast_artist_repo._reason_codes.get(artist.id) == ReasonCode.LOW_CONFIDENCE
    detail = broadcast_artist_repo._reason_details.get(artist.id)
    assert detail is not None
    assert detail  # non-empty formatted string


def test_match_artists_no_candidates_persists_no_candidates_reason() -> None:
    """When every strategy returns None, the service falls through to
    NEEDS_REVIEW with ReasonCode.NO_CANDIDATES."""
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()

    artist = _pending_artist("UNKNOWN BAND XYZ", broadcast_artist_repo, playlist_id)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=FakeBroadcastTrackIdentityRepository(),
        artist_repo=FakeArtistRepository(),
        match_repo=FakeMatchRepository(),
        rules_repo=FakeMappingRuleRepository(),
        mb_client=FakeMbClient(),
    )

    stored = broadcast_artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert broadcast_artist_repo._reason_codes.get(artist.id) == ReasonCode.NO_CANDIDATES
    assert broadcast_artist_repo._reason_details.get(artist.id) is not None


def test_match_artists_mb_hit_upserts_and_creates_match() -> None:
    """Covers old _try_mb_match: MB AUTO_MATCHED upserts the canonical and
    writes a MUSICBRAINZ_API-tier Match row."""
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    artist = _pending_artist("OZZY OSBOURNE", broadcast_artist_repo, playlist_id)
    mb_client = FakeMbClient(responses={
        "OZZY OSBOURNE": [
            {"id": "mbid-ozzy", "name": "Ozzy Osbourne",
             "sort-name": "Osbourne, Ozzy", "score": 100},
        ],
    })

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=FakeBroadcastTrackIdentityRepository(),
        artist_repo=artist_repo,
        match_repo=match_repo,
        rules_repo=FakeMappingRuleRepository(),
        mb_client=mb_client,
    )

    stored = broadcast_artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    assert artist_repo.get_by_id("mbid-ozzy") is not None
    created = match_repo.get_by_artist(artist.id)
    assert created is not None
    assert created.target_id == "mbid-ozzy"
    assert created.match_tier == MatchTier.MUSICBRAINZ_API


def test_match_artists_auto_rejected_cascades_to_identity_bulk_reject() -> None:
    """The AUTO_REJECTED cascade is preserved from legacy: identities under
    an AUTO_REJECTED artist are bulk-rejected at the end of the pass."""
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    track_identity_repo = FakeBroadcastTrackIdentityRepository()

    artist = _pending_artist("BAD ARTIST", broadcast_artist_repo, playlist_id)
    # Manually force AUTO_REJECTED — simulates a prior run's decision.
    broadcast_artist_repo.update_match_status(artist.id, MatchStatus.AUTO_REJECTED)

    identity = BroadcastTrackIdentity(
        id=uuid4(),
        broadcast_artist_id=artist.id,
        original_title="Song",
        normalized_title="song",
        normalized_signature="cascade_test_sig_artist_service",
    )
    track_identity_repo.upsert(identity)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=track_identity_repo,
        artist_repo=FakeArtistRepository(),
        match_repo=FakeMatchRepository(),
        rules_repo=FakeMappingRuleRepository(),
        mb_client=FakeMbClient(),
    )

    stored = track_identity_repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_REJECTED


# ---------------------------------------------------------------------------
# Step 1 — search coalescing
#
# coalesce_artist_searches builds {lower(original_name): [MbArtistResult, ...]}
# from a pending-artist list by calling search_artist once per distinct bucket.
# Mirrors coalesce_artist_lookups (mb_enrichment_tasks.py:192):
#   - transient httpx.HTTPError for a bucket -> key OMITTED (live fallback).
#   - empty MB response for a bucket -> key present with [] (no-candidates
#     sentinel; strategy returns None without re-querying).
#   - success -> key present with list of results.
# Representative query string per bucket = lex-first original_name whose
# .lower() hits the bucket.
# ---------------------------------------------------------------------------


class _RaisingMbClient(FakeMbClient):
    """FakeMbClient that raises httpx.HTTPError on selected search names."""

    def __init__(
        self,
        responses: dict[str, list[dict[str, object]]] | None = None,
        error_names: set[str] | None = None,
    ) -> None:
        super().__init__(responses=responses)  # type: ignore[arg-type]
        self._error_names = error_names or set()

    def search_artist(self, name: str) -> list[dict[str, object]]:
        self.calls.append(name)
        if name in self._error_names:
            raise httpx.ConnectError("simulated transient failure")
        return self._responses.get(name, [])  # type: ignore[return-value]


def test_coalesce_artist_searches_one_call_per_bucket() -> None:
    from backend.services.artist_matching_service import coalesce_artist_searches

    # 50 rows across 5 distinct name_keys (case variations of same underlying name).
    pending: list[BroadcastArtist] = []
    variants = ["Metallica", "metallica", "METALLICA"]  # all -> "metallica"
    for i in range(10):
        pending.append(BroadcastArtist(
            id=uuid4(), original_name=variants[i % 3], normalized_name="metallica",
        ))
    for name in ["Iron Maiden", "Slayer", "Anthrax", "Megadeth"]:
        for _ in range(10):
            pending.append(BroadcastArtist(
                id=uuid4(), original_name=name, normalized_name=name.lower(),
            ))
    assert len(pending) == 50

    mb = FakeMbClient(responses={
        "Metallica": [{"id": "m", "score": 100}],
        "Iron Maiden": [{"id": "im", "score": 100}],
        "Slayer": [{"id": "s", "score": 100}],
        "Anthrax": [{"id": "a", "score": 100}],
        "Megadeth": [{"id": "mg", "score": 100}],
    })
    search_map = coalesce_artist_searches(pending, mb)

    # Exactly 5 live calls — one per distinct lower()-bucket.
    assert len(mb.calls) == 5
    # All 5 buckets populated.
    assert set(search_map.keys()) == {
        "metallica", "iron maiden", "slayer", "anthrax", "megadeth",
    }


def test_coalesce_artist_searches_picks_lex_first_representative() -> None:
    # Three variants normalize to the same .lower() bucket; the representative
    # passed to search_artist must be deterministic (lex-first among originals).
    from backend.services.artist_matching_service import coalesce_artist_searches

    pending = [
        BroadcastArtist(id=uuid4(), original_name="metallica", normalized_name="metallica"),
        BroadcastArtist(id=uuid4(), original_name="Metallica", normalized_name="metallica"),
        BroadcastArtist(id=uuid4(), original_name="METALLICA", normalized_name="metallica"),
    ]
    mb = FakeMbClient(responses={
        "METALLICA": [{"id": "all-caps", "score": 100}],
        "Metallica": [{"id": "mixed", "score": 100}],
        "metallica": [{"id": "lower", "score": 100}],
    })
    coalesce_artist_searches(pending, mb)

    # Python's default sort on str is codepoint-order: upper A-Z < lower a-z.
    # So lex-first among {"metallica","Metallica","METALLICA"} is "METALLICA".
    assert mb.calls == ["METALLICA"]


def test_coalesce_artist_searches_omits_key_on_httpx_error() -> None:
    from backend.services.artist_matching_service import coalesce_artist_searches

    pending = [
        BroadcastArtist(id=uuid4(), original_name="BadBand", normalized_name="badband"),
        BroadcastArtist(id=uuid4(), original_name="GoodBand", normalized_name="goodband"),
    ]
    mb = _RaisingMbClient(
        responses={"GoodBand": [{"id": "g", "score": 100}]},
        error_names={"BadBand"},
    )
    search_map = coalesce_artist_searches(pending, mb)

    # Failing key is OMITTED (not present with any sentinel value).
    assert "badband" not in search_map
    # Successful key is present.
    assert search_map["goodband"] == [{"id": "g", "score": 100}]


def test_coalesce_artist_searches_inserts_empty_list_on_no_candidates() -> None:
    # MB returns 200 OK with zero results -> bucket present with [].
    from backend.services.artist_matching_service import coalesce_artist_searches

    pending = [
        BroadcastArtist(id=uuid4(), original_name="Nonexistent", normalized_name="nonexistent"),
    ]
    mb = FakeMbClient(responses={})  # default returns []
    search_map = coalesce_artist_searches(pending, mb)

    assert search_map == {"nonexistent": []}


def test_match_artists_coalesced_same_result_as_uncoalesced() -> None:
    # Equivalence: running with coalescing wired up (default post-change) must
    # produce the same per-row Match rows the per-row search_artist path would
    # have returned. Two distinct pending artists, two buckets (one search
    # per bucket in the pre-pass).
    #
    # Note: the duplicate-same-bucket scenario is pre-empted at the
    # broadcast_artists repo layer by upsert-on-normalized_name (normalize_artist
    # lower-cases + strips articles/features/etc., which collapses case-only
    # variants into a single row at write time). Bucket-dedup behavior of
    # coalesce_artist_searches itself is covered directly by
    # test_coalesce_artist_searches_one_call_per_bucket.
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    a1 = _pending_artist("Ozzy Osbourne", broadcast_artist_repo, playlist_id)
    a2 = _pending_artist("Slayer", broadcast_artist_repo, playlist_id)

    mb = FakeMbClient(responses={
        "Ozzy Osbourne": [
            {"id": "mbid-ozzy", "name": "Ozzy Osbourne",
             "sort-name": "Osbourne, Ozzy", "score": 100},
        ],
        "Slayer": [
            {"id": "mbid-slayer", "name": "Slayer", "sort-name": "Slayer", "score": 100},
        ],
    })

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=FakeBroadcastTrackIdentityRepository(),
        artist_repo=artist_repo,
        match_repo=match_repo,
        rules_repo=FakeMappingRuleRepository(),
        mb_client=mb,
    )

    for a, expected in [(a1, "mbid-ozzy"), (a2, "mbid-slayer")]:
        stored = broadcast_artist_repo.get_by_id(a.id)
        assert stored is not None
        assert stored.match_status == MatchStatus.AUTO_MATCHED
        created = match_repo.get_by_artist(a.id)
        assert created is not None
        assert created.target_id == expected

    # Exactly 2 live search_artist calls — one per distinct .lower() bucket,
    # all in the pre-pass. The per-row loop in the strategy short-circuits
    # via the populated search_map, making zero additional live calls.
    assert len(mb.calls) == 2


def test_match_artists_failure_isolation_pre_pass_httpx_error() -> None:
    # A transient pre-pass failure for one bucket must NOT block other buckets.
    # The failing bucket's rows fall through to per-row live search (which
    # will error again on the live path with this fake, but that's a per-row
    # failure surfaced by the existing error boundary, not a whole-batch abort).
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    good = _pending_artist("GoodBand", broadcast_artist_repo, playlist_id)
    _pending_artist("BadBand", broadcast_artist_repo, playlist_id)

    mb = _RaisingMbClient(
        responses={
            "GoodBand": [{"id": "mbid-good", "name": "GoodBand",
                          "sort-name": "GoodBand", "score": 100}],
        },
        error_names={"BadBand"},
    )

    # The pre-pass omits BadBand's bucket on httpx error. The per-row loop
    # then falls back to a live search_artist for BadBand which raises again;
    # whether that propagates out of match_artists_for_playlist is an
    # orthogonal concern (error boundary behavior unchanged by this task).
    # This test pins only the isolation property: GoodBand is resolved first
    # (alphabetic order over FakeBroadcastArtistRepository's iteration is
    # dict insertion order, GoodBand upserted first in this fixture).
    import contextlib

    with contextlib.suppress(httpx.HTTPError):
        match_artists_for_playlist(
            playlist_id=playlist_id,
            broadcast_artist_repo=broadcast_artist_repo,
            track_identity_repo=FakeBroadcastTrackIdentityRepository(),
            artist_repo=artist_repo,
            match_repo=match_repo,
            rules_repo=FakeMappingRuleRepository(),
            mb_client=mb,
        )

    # GoodBand resolved regardless of BadBand's pre-pass failure.
    good_stored = broadcast_artist_repo.get_by_id(good.id)
    assert good_stored is not None
    assert good_stored.match_status == MatchStatus.AUTO_MATCHED
    good_match = match_repo.get_by_artist(good.id)
    assert good_match is not None
    assert good_match.target_id == "mbid-good"


def test_match_artists_empty_bucket_skips_live_call() -> None:
    # A bucket that MB returned [] for in the pre-pass must NOT be retried
    # via live search during the per-row loop.
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    _pending_artist("NoSuchArtist", broadcast_artist_repo, playlist_id)
    mb = FakeMbClient(responses={})  # empty responses => [] for every name

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=FakeBroadcastTrackIdentityRepository(),
        artist_repo=artist_repo,
        match_repo=match_repo,
        rules_repo=FakeMappingRuleRepository(),
        mb_client=mb,
    )

    # search_artist called exactly ONCE — in the pre-pass. The per-row loop
    # reads the [] sentinel and does not re-query.
    assert len(mb.calls) == 1


def test_match_artists_emits_mb_task_summary() -> None:
    # match_artists_for_playlist emits a structured `mb_task_summary` event at
    # the end of each run. Tooling (automated loops, dashboards) asserts on
    # this event rather than diffing logs. Shape contract:
    #   task_type: "artist_matching"
    #   rows_processed, distinct_search_keys, distinct_mbids
    #   live_fetches_delta, cache_hits_delta
    #   duplicate_name_ratio (float in [0,1) or None for empty pending)
    #   duplicate_mbid_ratio = None (not used by matching task)
    from structlog.testing import capture_logs

    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    _pending_artist("Ozzy Osbourne", broadcast_artist_repo, playlist_id)
    _pending_artist("Slayer", broadcast_artist_repo, playlist_id)

    mb = FakeMbClient(responses={
        "Ozzy Osbourne": [{"id": "o", "name": "Ozzy", "sort-name": "Ozzy", "score": 100}],
        "Slayer": [{"id": "s", "name": "Slayer", "sort-name": "Slayer", "score": 100}],
    })
    # Simulate the concrete client's counter behavior so the test asserts a
    # real delta computation.
    orig_search = mb.search_artist

    def counting_search(name: str) -> list[dict[str, object]]:
        mb.live_fetches += 1
        return orig_search(name)

    mb.search_artist = counting_search  # type: ignore[method-assign]

    with capture_logs() as events:
        match_artists_for_playlist(
            playlist_id=playlist_id,
            broadcast_artist_repo=broadcast_artist_repo,
            track_identity_repo=FakeBroadcastTrackIdentityRepository(),
            artist_repo=artist_repo,
            match_repo=match_repo,
            rules_repo=FakeMappingRuleRepository(),
            mb_client=mb,
        )

    summary_events = [e for e in events if e.get("event") == "mb_task_summary"]
    assert len(summary_events) == 1
    s = summary_events[0]
    assert s["task_type"] == "artist_matching"
    assert s["rows_processed"] == 2
    assert s["distinct_search_keys"] == 2
    assert s["distinct_mbids"] == 0
    assert s["live_fetches_delta"] == 2  # one per distinct bucket
    assert s["cache_hits_delta"] == 0
    assert s["duplicate_name_ratio"] == 0.0  # 1 - 2/2
    assert s["duplicate_mbid_ratio"] is None


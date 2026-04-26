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

from typing import Any
from uuid import uuid4

import httpx

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.catalog import Artist
from backend.domain.enums import MatchStatus, MatchTier, ReasonCode, TargetType
from backend.domain.matching import MappingRule
from backend.services.artist_matching_service import match_artists_for_playlist
from backend.services.matching_constants import MB_SCORE_GAP
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
    assert stored.reason_code == ReasonCode.LOW_CONFIDENCE
    assert stored.reason_detail is not None
    assert stored.reason_detail  # non-empty formatted string


def test_match_artists_unresolved_non_truncated_persists_deferred_retry() -> None:
    """When every strategy returns None for a non-truncated name, the service
    routes to NEEDS_REVIEW/DEFERRED_RETRY (never NO_CANDIDATES). DEFERRED_RETRY
    is retryable on next ingestion; NO_CANDIDATES is permanent."""
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
    assert stored.reason_code == ReasonCode.DEFERRED_RETRY
    assert stored.reason_detail is not None


def test_match_artists_mb_hit_upserts_and_creates_match() -> None:
    """Covers old _try_mb_match: MB AUTO_MATCHED upserts the canonical and
    writes a MUSICBRAINZ_API-tier Match row. Uses a 30-char name so the
    Phase-2 truncation gate routes it to the MB tier."""
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    truncated = "OZZY OSBOURNE THE METAL LEGEND"  # 30 chars, alphanum end
    artist = _pending_artist(truncated, broadcast_artist_repo, playlist_id)
    mb_client = FakeMbClient(responses={
        truncated: [
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
        responses: dict[str, list[dict[str, Any]]] | None = None,
        error_names: set[str] | None = None,
    ) -> None:
        super().__init__(responses=responses)
        self._error_names = error_names or set()

    def search_artist(self, name: str) -> list[dict[str, Any]]:
        self.calls.append(name)
        if name in self._error_names:
            raise httpx.ConnectError("simulated transient failure")
        return self._responses.get(name, [])


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
    search_map, distinct_keys = coalesce_artist_searches(pending, mb)

    # Exactly 5 live calls — one per distinct lower()-bucket.
    assert len(mb.calls) == 5
    # All 5 buckets populated in both the map and the returned count.
    assert set(search_map.keys()) == {
        "metallica", "iron maiden", "slayer", "anthrax", "megadeth",
    }
    assert distinct_keys == 5


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
    # Tuple return discarded deliberately — this test pins call-site behavior
    # only, not the returned map/count.
    _ = coalesce_artist_searches(pending, mb)

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
    search_map, distinct_keys = coalesce_artist_searches(pending, mb)

    # Failing key is OMITTED (not present with any sentinel value).
    assert "badband" not in search_map
    # Successful key is present.
    assert search_map["goodband"] == [{"id": "g", "score": 100}]
    # Distinct count reflects the INPUT set (2 buckets), NOT the map (1 entry)
    # — so transient errors don't distort the pre-cache metric.
    assert distinct_keys == 2


def test_coalesce_artist_searches_inserts_empty_list_on_no_candidates() -> None:
    # MB returns 200 OK with zero results -> bucket present with [].
    from backend.services.artist_matching_service import coalesce_artist_searches

    pending = [
        BroadcastArtist(id=uuid4(), original_name="Nonexistent", normalized_name="nonexistent"),
    ]
    mb = FakeMbClient(responses={})  # default returns []
    search_map, _ = coalesce_artist_searches(pending, mb)

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

    # Use 30-char names so the Phase-2 truncation gate routes them to the MB tier.
    name_a = "OZZY OSBOURNE THE METAL LEGEND"  # 30 chars
    name_b = "SLAYER REIGN IN BLOOD ALBUM 86"   # 30 chars
    a1 = _pending_artist(name_a, broadcast_artist_repo, playlist_id)
    a2 = _pending_artist(name_b, broadcast_artist_repo, playlist_id)

    mb = FakeMbClient(responses={
        name_a: [
            {"id": "mbid-ozzy", "name": "Ozzy Osbourne",
             "sort-name": "Osbourne, Ozzy", "score": 100},
        ],
        name_b: [
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


def test_match_artists_empty_bucket_skips_live_call() -> None:
    # A bucket that MB returned [] for in the pre-pass must NOT be retried
    # via live search during the per-row loop.
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    # Truncated name so it routes through the MB tier (the whole point of
    # this test is the [] sentinel inside the search_map; non-truncated names
    # would skip MB entirely).
    _pending_artist("NoSuchArtistAaaaaaaaaaaaaaaaaa", broadcast_artist_repo, playlist_id)
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
    #   rows_queued, distinct_search_keys, distinct_mbids
    #   live_fetches_delta, cache_hits_delta
    #   duplicate_name_ratio (float in [0,1] or None for empty pending)
    #   duplicate_mbid_ratio = None (not used by matching task)
    from structlog.testing import capture_logs

    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    # Use 30-char (truncated) names so they route to the MB tier — the
    # whole point of this test is the live_fetches counter delta in the
    # mb_task_summary event.
    name_a = "Ozzy Osbourne The Metal Legen"  # 29 chars
    name_b = "Slayer Reign In Blood Album 80"  # 30 chars
    _pending_artist(name_a, broadcast_artist_repo, playlist_id)
    _pending_artist(name_b, broadcast_artist_repo, playlist_id)

    mb = FakeMbClient(responses={
        name_a: [{"id": "o", "name": "Ozzy", "sort-name": "Ozzy", "score": 100}],
        name_b: [{"id": "s", "name": "Slayer", "sort-name": "Slayer", "score": 100}],
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
    assert s["rows_queued"] == 2
    assert s["distinct_search_keys"] == 2
    assert s["distinct_mbids"] == 0
    assert s["live_fetches_delta"] == 2  # one per distinct bucket
    assert s["cache_hits_delta"] == 0
    assert s["duplicate_name_ratio"] == 0.0  # 1 - 2/2
    assert s["duplicate_mbid_ratio"] is None


def test_match_artists_summary_distinct_search_keys_stable_across_httpx_errors() -> None:
    # distinct_search_keys is computed over the INPUT set, not `len(search_map)`,
    # so a transient httpx error in the pre-pass (which omits one bucket from
    # the map) MUST NOT shift the metric. Ratios are pre-cache so they stay
    # stable across runs regardless of transient failure patterns.
    from structlog.testing import capture_logs

    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    # Use 30-char (truncated) names so they route to the MB tier — non-truncated
    # names skip MB entirely after Phase 2 and the httpx error path never fires.
    flaky = "Flaky Band That Almost Resolves"  # 31 chars
    good = "GoodBand With A Long Display Name"  # 33 chars
    _pending_artist(flaky, broadcast_artist_repo, playlist_id)
    _pending_artist(good, broadcast_artist_repo, playlist_id)

    mb = _RaisingMbClient(
        responses={good: [{"id": "g", "name": "GoodBand",
                           "sort-name": "GoodBand", "score": 100}]},
        error_names={flaky},
    )

    import contextlib

    with capture_logs() as events, contextlib.suppress(httpx.HTTPError):
        match_artists_for_playlist(
            playlist_id=playlist_id,
            broadcast_artist_repo=broadcast_artist_repo,
            track_identity_repo=FakeBroadcastTrackIdentityRepository(),
            artist_repo=artist_repo,
            match_repo=match_repo,
            rules_repo=FakeMappingRuleRepository(),
            mb_client=mb,
        )

    # The summary event is emitted from a finally block, so it fires even
    # when the per-row live fallback re-raises for the Flaky bucket. The
    # guarantee under test: distinct_search_keys counts the INPUT set (both
    # buckets), NOT the (smaller) search_map that the pre-pass populated.
    summary_events = [e for e in events if e.get("event") == "mb_task_summary"]
    assert len(summary_events) == 1
    assert summary_events[0]["distinct_search_keys"] == 2
    assert summary_events[0]["rows_queued"] == 2


def test_broadcast_artist_dataclass_carries_reason_code_after_update() -> None:
    """Reason state is on the dataclass, not in a sidecar. Tests assert via
    repo.get_by_id(...).reason_code, never via repo._reason_codes."""
    repo = FakeBroadcastArtistRepository()
    playlist_id = uuid4()
    artist = _pending_artist("ARTIST", repo, playlist_id)

    repo.update_match_status(
        artist.id,
        MatchStatus.NEEDS_REVIEW,
        reason_code=ReasonCode.LOW_CONFIDENCE,
        reason_detail="Score 55% — below confidence threshold",
    )

    stored = repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.reason_code == ReasonCode.LOW_CONFIDENCE
    assert stored.reason_detail == "Score 55% — below confidence threshold"


def test_reset_deferred_by_ids_promotes_only_deferred_returns_count() -> None:
    """Caller passes the artist IDs (typically derived from the current
    playlist's artist set), not names. ID-keying makes scope explicit and
    avoids accidental cross-playlist mutation if the API is later misused."""
    from backend.services.matching_reasons import format_deferred_retry

    repo = FakeBroadcastArtistRepository()
    playlist_id = uuid4()

    deferred = _pending_artist("ARTIST A", repo, playlist_id)
    repo.update_match_status(
        deferred.id, MatchStatus.NEEDS_REVIEW,
        reason_code=ReasonCode.DEFERRED_RETRY,
        reason_detail=format_deferred_retry(),
    )
    auto_matched = _pending_artist("ARTIST B", repo, playlist_id)
    repo.update_match_status(auto_matched.id, MatchStatus.AUTO_MATCHED)
    other_review = _pending_artist("ARTIST C", repo, playlist_id)
    repo.update_match_status(
        other_review.id, MatchStatus.NEEDS_REVIEW,
        reason_code=ReasonCode.LOW_CONFIDENCE,
        reason_detail="Score 55%",
    )
    # Out-of-scope row: DEFERRED_RETRY but its ID is NOT passed in, so it
    # must remain DEFERRED_RETRY (proves scope is honored).
    out_of_scope = _pending_artist("ARTIST D", repo, playlist_id)
    repo.update_match_status(
        out_of_scope.id, MatchStatus.NEEDS_REVIEW,
        reason_code=ReasonCode.DEFERRED_RETRY,
        reason_detail=format_deferred_retry(),
    )

    rows_reset = repo.reset_deferred_by_ids(
        [deferred.id, auto_matched.id, other_review.id]
    )

    assert rows_reset == 1
    assert repo.get_by_id(deferred.id).match_status == MatchStatus.PENDING  # type: ignore[union-attr]
    assert repo.get_by_id(deferred.id).reason_code is None  # type: ignore[union-attr]
    assert repo.get_by_id(auto_matched.id).match_status == MatchStatus.AUTO_MATCHED  # type: ignore[union-attr]
    assert repo.get_by_id(other_review.id).match_status == MatchStatus.NEEDS_REVIEW  # type: ignore[union-attr]
    assert repo.get_by_id(out_of_scope.id).match_status == MatchStatus.NEEDS_REVIEW  # type: ignore[union-attr]
    assert repo.get_by_id(out_of_scope.id).reason_code == ReasonCode.DEFERRED_RETRY  # type: ignore[union-attr]


def test_reset_deferred_by_ids_empty_input_returns_zero() -> None:
    repo = FakeBroadcastArtistRepository()
    assert repo.reset_deferred_by_ids([]) == 0


def test_mb_auto_matched_triggers_catalog_upsert_from_orchestration() -> None:
    """Invariant 1 (orchestration owns writes). The MB upsert moved from
    MusicBrainzApiStrategy into match_artists_for_playlist's result loop.
    Verify the orchestration calls upsert_musicbrainz_artist with the
    canonical kwargs whenever a result has mb_candidate AND
    status == AUTO_MATCHED."""
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    track_identity_repo = FakeBroadcastTrackIdentityRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()
    rules_repo = FakeMappingRuleRepository()

    # 30-char name so the Phase-2 truncation gate routes it to the MB tier.
    name = "Resolved Name With Long Display"  # 31 chars
    artist = _pending_artist(name, broadcast_artist_repo, playlist_id)
    mb_client = FakeMbClient(responses={name: [
        {"id": "mbid-x", "name": "Resolved Name", "score": 100,
         "sort-name": "Name, Resolved", "disambiguation": "rock band"},
    ]})

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=track_identity_repo,
        artist_repo=artist_repo,
        match_repo=match_repo,
        rules_repo=rules_repo,
        mb_client=mb_client,
    )

    # Sanity: artist landed AUTO_MATCHED.
    stored = broadcast_artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED

    upserts = artist_repo.musicbrainz_upserts
    assert len(upserts) == 1
    assert upserts[0]["mbid"] == "mbid-x"
    assert upserts[0]["name"] == "Resolved Name"
    assert upserts[0]["sort_name"] == "Name, Resolved"
    assert upserts[0]["normalized_name"] == normalize_artist("Resolved Name")
    assert upserts[0]["disambiguation"] == "rock band"


# ---------------------------------------------------------------------------
# Phase-2 orchestration: truncated-only MB + DeferredRetryStrategy fallback
# ---------------------------------------------------------------------------


def test_orchestration_non_truncated_unresolved_persists_deferred_retry_no_mb_call() -> None:
    """Invariant 3: MB is consulted only for likely-truncated names.
    A clean (non-truncated) name with no local candidates parks in
    DEFERRED_RETRY without ever calling MB."""
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    track_identity_repo = FakeBroadcastTrackIdentityRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()
    rules_repo = FakeMappingRuleRepository()
    mb_client = FakeMbClient()

    artist = _pending_artist("U2", broadcast_artist_repo, playlist_id)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=track_identity_repo,
        artist_repo=artist_repo,
        match_repo=match_repo,
        rules_repo=rules_repo,
        mb_client=mb_client,
    )

    stored = broadcast_artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.reason_code == ReasonCode.DEFERRED_RETRY
    assert mb_client.calls == []  # Invariant 3


def test_orchestration_truncated_unresolved_calls_mb() -> None:
    """A truncated name still gets the full MB tier."""
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    track_identity_repo = FakeBroadcastTrackIdentityRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()
    rules_repo = FakeMappingRuleRepository()

    truncated = "A Very Long Truncated Artist"  # 28 chars; within tolerance of 30
    artist = _pending_artist(truncated, broadcast_artist_repo, playlist_id)
    mb_client = FakeMbClient(responses={truncated: [
        {"id": "mb-1", "name": "A Very Long Truncated Artist Name", "score": 96,
         "sort-name": "Truncated Name", "disambiguation": ""},
    ]})

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=track_identity_repo,
        artist_repo=artist_repo,
        match_repo=match_repo,
        rules_repo=rules_repo,
        mb_client=mb_client,
    )

    stored = broadcast_artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    created = match_repo.get_by_artist(artist.id)
    assert created is not None
    assert created.target_id == "mb-1"
    assert len(artist_repo.musicbrainz_upserts) == 1


def test_orchestration_truncated_with_no_mb_candidates_falls_to_deferred() -> None:
    """Invariant 2: every artist gets a terminal result. Truncated + no MB hits → DEFERRED_RETRY."""
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    track_identity_repo = FakeBroadcastTrackIdentityRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()
    rules_repo = FakeMappingRuleRepository()
    mb_client = FakeMbClient()  # no responses → empty for every key

    truncated = "Z" * 30
    artist = _pending_artist(truncated, broadcast_artist_repo, playlist_id)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=track_identity_repo,
        artist_repo=artist_repo,
        match_repo=match_repo,
        rules_repo=rules_repo,
        mb_client=mb_client,
    )

    stored = broadcast_artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.reason_code == ReasonCode.DEFERRED_RETRY


def test_orchestration_local_match_always_wins_over_truncation_check() -> None:
    """Phase 1 runs for every artist regardless of truncation. A truncated
    name that resolves locally never reaches the MB tier."""
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    track_identity_repo = FakeBroadcastTrackIdentityRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()
    rules_repo = FakeMappingRuleRepository()
    mb_client = FakeMbClient()

    truncated = "The Beatles That Are Awesome A"  # 30 chars
    artist_repo.upsert(Artist(
        id="local-beatles",
        name=truncated,
        sort_name=truncated,
        normalized_name=normalize_artist(truncated),
        mbid="mbid-beatles",
    ))
    artist = _pending_artist(truncated, broadcast_artist_repo, playlist_id)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=track_identity_repo,
        artist_repo=artist_repo,
        match_repo=match_repo,
        rules_repo=rules_repo,
        mb_client=mb_client,
    )

    stored = broadcast_artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    assert mb_client.calls == []

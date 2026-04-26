from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

import httpx
import structlog
from rapidfuzz import fuzz

from backend.domain.broadcast import BroadcastArtist
from backend.domain.catalog import Artist
from backend.domain.enums import MatchStatus, MatchTier, ReasonCode, TargetType
from backend.domain.matching import MappingRule, Match
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.broadcast_artists import BroadcastArtistRepository
from backend.repositories.broadcast_track_identities import BroadcastTrackIdentityRepository
from backend.repositories.mapping_rules import MappingRuleRepository
from backend.repositories.matches import MatchRepository
from backend.services.matching_constants import (
    MB_AUTO_LINK_SCORE,
    MB_MIN_CANDIDATE_SCORE,
    MB_SCORE_GAP,
    MID_BAND_GAP_THRESHOLD,
    MID_BAND_LOWER,
    MID_BAND_UPPER,
    MIN_PRESENTATION_SCORE,
)
from backend.services.matching_reasons import (
    format_ambiguous_gap,
    format_low_confidence,
)
from backend.services.matching_utils import rule_matches
from backend.services.mb_client import MusicBrainzClientProtocol
from backend.services.mb_types import MbArtistResult
from backend.services.normalization import normalize_artist

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Strategy pattern (PR 4).
# Strategies produce ArtistMatchResult values; the service function
# (match_artists_for_playlist) owns all broadcast_artists / matches writes.
# Exception: MusicBrainzApiStrategy promotes MB results into the local artist
# catalog via upsert_musicbrainz_artist on AUTO_MATCHED — this is a deliberate
# read-through cache side effect so future NormalizationStrategy runs can hit
# locally. The engine walks strategies in order and returns the first non-None.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtistMatchResult:
    """Immutable result returned by an ArtistMatchingStrategy.

    target_id is always the artist MBID — required by the downstream MBID-graph
    identity tier (ResolvedArtistMbidStrategy), which calls
    library_file_repo.get_by_artist_mbid(Match.target_id). NormalizationStrategy
    filters out canonicals without an mbid to uphold this contract;
    MusicBrainzApiStrategy uses the MB API's id; MappingRuleStrategy relies on
    rules storing MBIDs by convention.

    reason_code / reason_detail are None for AUTO_MATCHED; populated for
    NEEDS_REVIEW using the stable ReasonCode vocabulary.

    mb_candidate carries the raw MB search result for the orchestration to
    upsert into the local catalog when status == AUTO_MATCHED. Populated only
    by MusicBrainzApiStrategy; None for all other strategies. Decoupling the
    upsert from the strategy keeps the strategy single-responsibility (decide
    the match) and lets the orchestration own all writes.
    """

    status: MatchStatus
    tier: MatchTier
    confidence_score: float
    target_id: str
    reason_code: ReasonCode | None = None
    reason_detail: str | None = None
    mb_candidate: MbArtistResult | None = None


class ArtistMatchingStrategy(Protocol):
    """One resolution tier for a BroadcastArtist."""

    def apply(self, broadcast_artist: BroadcastArtist) -> ArtistMatchResult | None: ...


def _decide_artist_zone(
    top_score: float,
    gap: float,
    strong_match_threshold: int,
    score_gap: int,
    has_competitor: bool,
    mb_auto_link_score: int = MB_AUTO_LINK_SCORE,
) -> tuple[MatchStatus, ReasonCode | None, str | None]:
    """Four-zone decision from matching_constants. Shared between
    NormalizationStrategy and MusicBrainzApiStrategy to avoid duplication
    while keeping each strategy's structure flat and readable.

    `has_competitor` guards BOTH gap-dependent auto-match clauses: when
    there is no real second candidate, gap is synthesized (100 locally,
    or top_score for MB where second=0). Without the guard, a lone
    candidate at score 85 would auto-match via the strong_match_threshold+gap
    clause (synthetic gap >> MB_SCORE_GAP), which is exactly the "too
    permissive" bug the mid-band guard was added to prevent. Apply the
    same guard on both gap-based clauses so a lone candidate always
    needs human review, regardless of the band.

    `mb_auto_link_score` is operator-tunable (from settings); defaults to the
    module constant. This is the unconditional-AUTO gate (gap irrelevant,
    no competitor check — at >= 95 score the token match is effectively
    a literal identity and a lone result is still trustworthy).
    """
    auto_match = (
        top_score >= mb_auto_link_score
        or (
            has_competitor
            and top_score >= strong_match_threshold
            and gap >= score_gap
        )
        or (
            has_competitor
            and MID_BAND_LOWER <= top_score <= MID_BAND_UPPER
            and gap >= MID_BAND_GAP_THRESHOLD
        )
    )
    if auto_match:
        return MatchStatus.AUTO_MATCHED, None, None
    # AMBIGUOUS_GAP only applies when there's a real peer that's close on
    # score. A lone candidate (has_competitor=False) with synthetic gap=100
    # is NOT ambiguous — there's nobody to be ambiguous with. Report it as
    # LOW_CONFIDENCE so format_ambiguous_gap's "top candidates within 100
    # points" message never renders.
    if has_competitor and top_score >= strong_match_threshold:
        return (
            MatchStatus.NEEDS_REVIEW,
            ReasonCode.AMBIGUOUS_GAP,
            format_ambiguous_gap(gap, float(score_gap)),
        )
    return (
        MatchStatus.NEEDS_REVIEW,
        ReasonCode.LOW_CONFIDENCE,
        format_low_confidence(top_score),
    )


class MappingRuleStrategy:
    """Global mapping-rule override for artists."""

    def __init__(self, rules: list[MappingRule]) -> None:
        self._rules = rules

    def apply(self, broadcast_artist: BroadcastArtist) -> ArtistMatchResult | None:
        for rule in self._rules:
            if rule.target_type != TargetType.ARTIST:
                continue
            if not rule_matches(rule.source_pattern, broadcast_artist.normalized_name):
                continue
            return ArtistMatchResult(
                status=MatchStatus.AUTO_MATCHED,
                tier=MatchTier.MANUAL,
                confidence_score=100.0,
                target_id=rule.target_id,
            )
        return None


class NormalizationStrategy:
    """Local canonical catalog match — exact normalized-name hit or fuzzy.

    Exact hit produces score=100 immediately (no rapidfuzz needed). Otherwise
    rapidfuzz.token_sort_ratio against each canonical name. Applies the
    four-zone threshold logic from matching_constants.
    """

    def __init__(
        self,
        all_canonical: list[Artist],
        strong_match_threshold: int = 80,
        mb_score_gap: int = MB_SCORE_GAP,
        mb_auto_link_score: int = MB_AUTO_LINK_SCORE,
        min_presentation_score: int = MIN_PRESENTATION_SCORE,
    ) -> None:
        self._all_canonical = all_canonical
        self._strong_match_threshold = strong_match_threshold
        self._gap = mb_score_gap
        self._mb_auto_link_score = mb_auto_link_score
        self._min_presentation_score = min_presentation_score

    def apply(self, broadcast_artist: BroadcastArtist) -> ArtistMatchResult | None:
        if not self._all_canonical:
            return None

        target = broadcast_artist.normalized_name
        # Exact pass — only MBID-bearing canonicals are usable downstream by
        # the identity-tier MBID-graph lookup (get_by_artist_mbid(target_id)).
        # A local-only canonical (mbid=None) must fall through so the engine
        # can reach MusicBrainzApiStrategy.
        for c in self._all_canonical:
            if c.mbid is None:
                continue
            if normalize_artist(c.name) == target:
                return ArtistMatchResult(
                    status=MatchStatus.AUTO_MATCHED,
                    tier=MatchTier.NORMALIZATION,
                    confidence_score=100.0,
                    target_id=c.mbid,
                )

        # Fuzzy pass — restrict to MBID-bearing canonicals. If none exist,
        # fall through so MusicBrainzApiStrategy can try.
        with_mbid = [c for c in self._all_canonical if c.mbid is not None]
        if not with_mbid:
            return None

        # Sort key: (score DESC, mbid ASC) for deterministic tie-breaking.
        # Without the mbid secondary key, two canonicals with identical fuzzy
        # scores would be ordered by DB iteration order, making best/gap
        # non-deterministic across runs.
        scored: list[tuple[float, Artist]] = sorted(
            (
                (float(fuzz.token_sort_ratio(target, normalize_artist(c.name))), c)
                for c in with_mbid
            ),
            key=lambda x: (-x[0], x[1].mbid or ""),
        )
        top_score, best = scored[0]
        # Noise floor: if the best fuzzy score is below the configured
        # min_presentation_score, fall through so the engine can reach
        # MusicBrainzApiStrategy. Mirrors the legacy _try_fuzzy_match's
        # return-False-below-threshold behaviour.
        if top_score < self._min_presentation_score:
            return None
        has_competitor = len(scored) > 1
        gap = (top_score - scored[1][0]) if has_competitor else 100.0

        status, rc, rd = _decide_artist_zone(
            top_score, gap, self._strong_match_threshold, self._gap, has_competitor,
            mb_auto_link_score=self._mb_auto_link_score,
        )
        assert best.mbid is not None  # guaranteed by the with_mbid filter above
        return ArtistMatchResult(
            status=status,
            tier=MatchTier.NORMALIZATION,
            confidence_score=top_score,
            target_id=best.mbid,
            reason_code=rc,
            reason_detail=rd,
        )


class MusicBrainzApiStrategy:
    """Final artist tier — query MusicBrainz.

    Read-only: returns ArtistMatchResult with mb_candidate populated whenever
    a candidate is selected. The orchestration is responsible for upserting
    the MB result into the local catalog when status == AUTO_MATCHED so the
    strategy stays single-responsibility (decide the match) and all writes
    flow through one layer.

    `search_map` short-circuits the live search. When present, bucket key is
    `broadcast_artist.original_name.lower()` — identical to the cache key
    suffix `artist-search:{name.lower()}` used by MusicBrainzApiClient.
    Sentinel convention:
      - key absent  -> live search_artist fallback (same as pre-coalesce).
      - key = []    -> "no candidates" from MB, return None without live call.
      - key = [...] -> use those results directly.
    The map is typically populated by `coalesce_artist_searches` before the
    per-row loop in `match_artists_for_playlist`.
    """

    def __init__(
        self,
        mb_client: MusicBrainzClientProtocol,
        strong_match_threshold: int = 80,
        mb_score_gap: int = MB_SCORE_GAP,
        mb_auto_link_score: int = MB_AUTO_LINK_SCORE,
        search_map: dict[str, list[MbArtistResult]] | None = None,
    ) -> None:
        self._mb = mb_client
        self._strong_match_threshold = strong_match_threshold
        self._gap = mb_score_gap
        self._mb_auto_link_score = mb_auto_link_score
        self._search_map = search_map

    def apply(self, broadcast_artist: BroadcastArtist) -> ArtistMatchResult | None:
        name_key = broadcast_artist.original_name.lower()
        if self._search_map is not None and name_key in self._search_map:
            results = self._search_map[name_key]
        else:
            results = self._mb.search_artist(broadcast_artist.original_name)
        if not results:
            return None

        candidates: list[MbArtistResult] = [
            r for r in results if r.get("score", 0) >= MB_MIN_CANDIDATE_SCORE
        ]
        if not candidates:
            return None

        # Sort key: (score DESC, id ASC) for deterministic tie-breaking across
        # runs. MB API typically returns score-sorted results but does not
        # guarantee ordering among equal-scored candidates.
        candidates.sort(key=lambda r: (-r.get("score", 0), r.get("id", "")))
        best = candidates[0]
        has_competitor = len(candidates) > 1
        second = candidates[1].get("score", 0) if has_competitor else 0
        top_score = float(best.get("score", 0))
        gap = float(best.get("score", 0) - second)

        status, rc, rd = _decide_artist_zone(
            top_score, gap, self._strong_match_threshold, self._gap, has_competitor,
            mb_auto_link_score=self._mb_auto_link_score,
        )

        return ArtistMatchResult(
            status=status,
            tier=MatchTier.MUSICBRAINZ_API,
            confidence_score=top_score,
            target_id=best["id"],
            reason_code=rc,
            reason_detail=rd,
            mb_candidate=best,
        )


class ArtistMatchingEngine:
    """Iterates strategies in order; returns first non-None result.

    No persistence — the service function (match_artists_for_playlist) owns
    all DB writes.
    """

    def __init__(self, strategies: list[ArtistMatchingStrategy]) -> None:
        self._strategies = strategies

    def resolve(self, broadcast_artist: BroadcastArtist) -> ArtistMatchResult | None:
        for strategy in self._strategies:
            result = strategy.apply(broadcast_artist)
            if result is not None:
                return result
        return None


def coalesce_artist_searches(
    pending: list[BroadcastArtist],
    client: MusicBrainzClientProtocol,
) -> tuple[dict[str, list[MbArtistResult]], int]:
    """Call search_artist exactly once per distinct `original_name.lower()`.

    Returns `(search_map, distinct_key_count)`. The count is the pre-cache
    bucket count — stable even when transient httpx errors omit keys from
    `search_map` — so callers can report it as `distinct_search_keys` in
    observability events without a second scan of `pending`.

    Sentinel convention (same as coalesce_artist_lookups):
      - transient httpx.HTTPError -> OMIT the key (per-row live fallback
        reproduces the failure in isolation).
      - empty MB response -> key present with [] (strategy short-circuits
        to None without re-querying).
      - success -> key present with list of results.

    Bucket key is `original_name.lower()` to match the cache key suffix
    `artist-search:{name.lower()}` used by MusicBrainzApiClient. Choice of
    representative query string within a bucket is irrelevant to the cache
    (always `.lower()`-keyed) and to MB's search (case-insensitive); the
    lex-first original is chosen purely for deterministic test behavior.
    """
    buckets: dict[str, list[str]] = {}
    for row in pending:
        buckets.setdefault(row.original_name.lower(), []).append(row.original_name)

    result: dict[str, list[MbArtistResult]] = {}
    for name_key, originals in buckets.items():
        representative = sorted(originals)[0]
        try:
            result[name_key] = client.search_artist(representative)
        except httpx.HTTPError as exc:
            logger.warning(
                "mb_coalesce_search_failed",
                name_key=name_key,
                representative=representative,
                error=str(exc),
            )
    return result, len(buckets)


def match_artists_for_playlist(
    playlist_id: UUID,
    broadcast_artist_repo: BroadcastArtistRepository,
    track_identity_repo: BroadcastTrackIdentityRepository,
    artist_repo: ArtistCatalogRepository,
    match_repo: MatchRepository,
    rules_repo: MappingRuleRepository,
    mb_client: MusicBrainzClientProtocol,
    strong_match_threshold: int = 80,
    mb_score_gap: int = MB_SCORE_GAP,
    mb_auto_link_score: int = MB_AUTO_LINK_SCORE,
    min_presentation_score: int = MIN_PRESENTATION_SCORE,
) -> None:
    """Resolve PENDING artists for a playlist. Strategy order:
    mapping rules -> normalization (exact + fuzzy local catalog) -> MusicBrainz API.

    Persistence lives here, not in strategies. Strategies return
    ArtistMatchResult values; this function writes the match row and status
    transition.

    AUTO_REJECTED cascade: after all pending are resolved, identities under an
    AUTO_REJECTED artist are bulk-rejected (preserved from legacy).
    """
    pending = broadcast_artist_repo.get_pending_for_playlist(playlist_id)
    rules = rules_repo.list_ordered()
    all_canonical = artist_repo.list_all()

    live_start = mb_client.live_fetches
    hits_start = mb_client.cache_hits

    search_map, distinct_search_keys = coalesce_artist_searches(pending, mb_client)

    engine = ArtistMatchingEngine([
        MappingRuleStrategy(rules),
        NormalizationStrategy(
            all_canonical, strong_match_threshold, mb_score_gap, mb_auto_link_score,
            min_presentation_score=min_presentation_score,
        ),
        MusicBrainzApiStrategy(
            mb_client, strong_match_threshold, mb_score_gap, mb_auto_link_score,
            search_map=search_map,
        ),
    ])

    try:
        for broadcast_artist in pending:
            result = engine.resolve(broadcast_artist)
            if result is None:
                broadcast_artist_repo.update_match_status(
                    broadcast_artist.id,
                    MatchStatus.NEEDS_REVIEW,
                    reason_code=ReasonCode.NO_CANDIDATES,
                    reason_detail="No candidates found across all matching tiers",
                )
                continue

            broadcast_artist_repo.update_match_status(
                broadcast_artist.id,
                result.status,
                reason_code=result.reason_code,
                reason_detail=result.reason_detail,
            )

            if result.status == MatchStatus.AUTO_MATCHED:
                match_repo.create(Match(
                    id=uuid4(),
                    artist_id=broadcast_artist.id,
                    target_id=result.target_id,
                    target_type=TargetType.ARTIST,
                    confidence_score=result.confidence_score,
                    match_tier=result.tier,
                ))
                # Catalog upsert moved out of MusicBrainzApiStrategy (SRP).
                # Strategies decide; orchestration writes. Only the MB tier
                # populates mb_candidate, so this fires only on MB hits.
                if result.mb_candidate is not None:
                    artist_repo.upsert_musicbrainz_artist(
                        mbid=result.mb_candidate["id"],
                        name=result.mb_candidate["name"],
                        sort_name=result.mb_candidate.get(
                            "sort-name", result.mb_candidate["name"]
                        ),
                        normalized_name=normalize_artist(result.mb_candidate["name"]),
                        disambiguation=result.mb_candidate.get("disambiguation"),
                    )

        _cascade_auto_rejected(playlist_id, broadcast_artist_repo, track_identity_repo)
    finally:
        # Emit in finally so observability survives exceptions raised from
        # the engine or the cascade. `distinct_search_keys` comes from the
        # coalesce pre-pass so it counts the input set, not `len(search_map)`
        # — a pre-pass failure that omits a bucket must not shift the metric.
        # `rows_queued` is the input size (before the resolve loop), not a
        # completion count — on a partial-failure run it still reflects what
        # was submitted.
        rows_queued = len(pending)
        logger.info(
            "mb_task_summary",
            task_type="artist_matching",
            rows_queued=rows_queued,
            distinct_search_keys=distinct_search_keys,
            distinct_mbids=0,
            live_fetches_delta=mb_client.live_fetches - live_start,
            cache_hits_delta=mb_client.cache_hits - hits_start,
            # None only when there's no input. With input and zero distinct
            # keys the ratio is well-defined (1.0); the caller can distinguish
            # "no data" from "no diversity" by the sign of rows_queued.
            duplicate_name_ratio=(
                1.0 - (distinct_search_keys / rows_queued) if rows_queued else None
            ),
            duplicate_mbid_ratio=None,
        )


def _cascade_auto_rejected(
    playlist_id: UUID,
    broadcast_artist_repo: BroadcastArtistRepository,
    track_identity_repo: BroadcastTrackIdentityRepository,
) -> None:
    """Cascade: AUTO_REJECTED artists -> bulk reject child identities.

    Preserved from the legacy implementation.
    """
    all_playlist_artists = broadcast_artist_repo.get_all_for_playlist(playlist_id)
    for broadcast_artist in all_playlist_artists:
        if broadcast_artist.match_status == MatchStatus.AUTO_REJECTED:
            track_identity_repo.bulk_reject_by_artist(broadcast_artist.id)

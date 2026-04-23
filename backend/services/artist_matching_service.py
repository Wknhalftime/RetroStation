from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

import structlog
from rapidfuzz import fuzz

from backend.domain.broadcast import BroadcastArtist
from backend.domain.catalog import Artist
from backend.domain.enums import MatchStatus, MatchTier, TargetType
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
    ReasonCode,
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
# Strategies produce ArtistMatchResult values; persistence lives in the
# service function (match_artists_for_playlist). The engine walks strategies
# in order and returns the first non-None result.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtistMatchResult:
    """Immutable result returned by an ArtistMatchingStrategy.

    Strategies produce values; they do not persist. target_id is an MBID for
    catalog/MB matches, or a canonical artist UUID for normalization hits.
    reason_code / reason_detail are None for AUTO_MATCHED; populated for
    NEEDS_REVIEW using the stable ReasonCode vocabulary.
    """

    status: MatchStatus
    tier: MatchTier
    confidence_score: float
    target_id: str
    reason_code: ReasonCode | None = None
    reason_detail: str | None = None


class ArtistMatchingStrategy(Protocol):
    """One resolution tier for a BroadcastArtist."""

    def apply(self, broadcast_artist: BroadcastArtist) -> ArtistMatchResult | None: ...


def _decide_artist_zone(
    top_score: float,
    gap: float,
    high_threshold: int,
    score_gap: int,
    has_competitor: bool,
) -> tuple[MatchStatus, ReasonCode | None, str | None]:
    """Four-zone decision from matching_constants. Shared between
    NormalizationStrategy and MusicBrainzApiStrategy to avoid duplication
    while keeping each strategy's structure flat and readable.

    `has_competitor` mirrors the identity side: when there is no real
    second candidate, gap is synthesized (100 locally, or top_score for MB
    where second=0), so the mid-band clause must NOT auto-match a lone
    candidate. This matches the guard in
    `identity_matching_service._score_candidates`.
    """
    auto_match = (
        top_score >= MB_AUTO_LINK_SCORE
        or (top_score >= high_threshold and gap >= score_gap)
        or (
            has_competitor
            and MID_BAND_LOWER <= top_score <= MID_BAND_UPPER
            and gap >= MID_BAND_GAP_THRESHOLD
        )
    )
    if auto_match:
        return MatchStatus.AUTO_MATCHED, None, None
    if top_score >= MIN_PRESENTATION_SCORE:
        if top_score >= high_threshold:
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
        high_threshold: int = 80,
        mb_score_gap: int = MB_SCORE_GAP,
    ) -> None:
        self._all_canonical = all_canonical
        self._high_threshold = high_threshold
        self._gap = mb_score_gap

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

        scored: list[tuple[float, Artist]] = sorted(
            (
                (float(fuzz.token_sort_ratio(target, normalize_artist(c.name))), c)
                for c in with_mbid
            ),
            key=lambda x: x[0],
            reverse=True,
        )
        top_score, best = scored[0]
        # Noise floor: if the best fuzzy score is below MIN_PRESENTATION_SCORE,
        # fall through so the engine can reach MusicBrainzApiStrategy. Mirrors
        # the legacy _try_fuzzy_match's return-False-below-threshold behaviour.
        if top_score < MIN_PRESENTATION_SCORE:
            return None
        has_competitor = len(scored) > 1
        gap = (top_score - scored[1][0]) if has_competitor else 100.0

        status, rc, rd = _decide_artist_zone(
            top_score, gap, self._high_threshold, self._gap, has_competitor
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
    """Final artist tier — query MusicBrainz. Caches AUTO_MATCHED results to
    local artist catalog so subsequent NormalizationStrategy runs hit locally.

    Preserves the legacy _try_mb_match side effect: on AUTO_MATCHED, upserts
    the MB result into the local Artist catalog.
    """

    def __init__(
        self,
        mb_client: MusicBrainzClientProtocol,
        artist_repo: ArtistCatalogRepository,
        high_threshold: int = 80,
        mb_score_gap: int = MB_SCORE_GAP,
    ) -> None:
        self._mb = mb_client
        self._catalog = artist_repo
        self._high_threshold = high_threshold
        self._gap = mb_score_gap

    def apply(self, broadcast_artist: BroadcastArtist) -> ArtistMatchResult | None:
        results = self._mb.search_artist(broadcast_artist.original_name)
        if not results:
            return None

        candidates: list[MbArtistResult] = [
            r for r in results if r.get("score", 0) >= MB_MIN_CANDIDATE_SCORE
        ]
        if not candidates:
            return None

        candidates.sort(key=lambda r: r.get("score", 0), reverse=True)
        best = candidates[0]
        has_competitor = len(candidates) > 1
        second = candidates[1].get("score", 0) if has_competitor else 0
        top_score = float(best.get("score", 0))
        gap = float(best.get("score", 0) - second)

        status, rc, rd = _decide_artist_zone(
            top_score, gap, self._high_threshold, self._gap, has_competitor
        )

        # Preserve legacy side effect: cache the MB result into the local
        # catalog on AUTO_MATCHED so later NormalizationStrategy runs can
        # hit locally. Use upsert_musicbrainz_artist so mbid / origin /
        # normalized_name are populated — the generic upsert leaves them
        # NULL / LOCAL, which breaks later lookups keyed on those columns.
        if status == MatchStatus.AUTO_MATCHED:
            self._catalog.upsert_musicbrainz_artist(
                mbid=best["id"],
                name=best["name"],
                sort_name=best.get("sort-name", best["name"]),
                normalized_name=normalize_artist(best["name"]),
                disambiguation=best.get("disambiguation"),
            )

        return ArtistMatchResult(
            status=status,
            tier=MatchTier.MUSICBRAINZ_API,
            confidence_score=top_score,
            target_id=best["id"],
            reason_code=rc,
            reason_detail=rd,
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


def match_artists_for_playlist(
    playlist_id: UUID,
    broadcast_artist_repo: BroadcastArtistRepository,
    track_identity_repo: BroadcastTrackIdentityRepository,
    artist_repo: ArtistCatalogRepository,
    match_repo: MatchRepository,
    rules_repo: MappingRuleRepository,
    mb_client: MusicBrainzClientProtocol,
    high_threshold: int = 80,
    mb_score_gap: int = MB_SCORE_GAP,
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

    engine = ArtistMatchingEngine([
        MappingRuleStrategy(rules),
        NormalizationStrategy(all_canonical, high_threshold, mb_score_gap),
        MusicBrainzApiStrategy(mb_client, artist_repo, high_threshold, mb_score_gap),
    ])

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

    _cascade_auto_rejected(playlist_id, broadcast_artist_repo, track_identity_repo)


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

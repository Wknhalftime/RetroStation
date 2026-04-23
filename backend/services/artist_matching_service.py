from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple, Protocol
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
# Strategy pattern — PR 4 Tasks 1-4.
# Legacy `_try_*` functions below remain in place until Task 5 removes them.
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
) -> tuple[MatchStatus, ReasonCode | None, str | None]:
    """Four-zone decision from matching_constants. Shared between
    NormalizationStrategy and MusicBrainzApiStrategy to avoid duplication
    while keeping each strategy's structure flat and readable.
    """
    auto_match = (
        top_score >= MB_AUTO_LINK_SCORE
        or (top_score >= high_threshold and gap >= score_gap)
        or (
            MID_BAND_LOWER <= top_score <= MID_BAND_UPPER
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
        mb_auto_link_score: int = MB_AUTO_LINK_SCORE,
        mb_score_gap: int = MB_SCORE_GAP,
    ) -> None:
        self._all_canonical = all_canonical
        self._high = mb_auto_link_score
        self._gap = mb_score_gap

    def apply(self, broadcast_artist: BroadcastArtist) -> ArtistMatchResult | None:
        if not self._all_canonical:
            return None

        target = broadcast_artist.normalized_name
        # Exact pass
        for c in self._all_canonical:
            if normalize_artist(c.name) == target:
                return ArtistMatchResult(
                    status=MatchStatus.AUTO_MATCHED,
                    tier=MatchTier.NORMALIZATION,
                    confidence_score=100.0,
                    target_id=c.id,
                )

        # Fuzzy pass
        scored: list[tuple[float, Artist]] = sorted(
            (
                (float(fuzz.token_sort_ratio(target, normalize_artist(c.name))), c)
                for c in self._all_canonical
            ),
            key=lambda x: x[0],
            reverse=True,
        )
        top_score, best = scored[0]
        gap = (top_score - scored[1][0]) if len(scored) > 1 else 100.0

        status, rc, rd = _decide_artist_zone(
            top_score, gap, self._high, self._gap
        )
        return ArtistMatchResult(
            status=status,
            tier=MatchTier.NORMALIZATION,
            confidence_score=top_score,
            target_id=best.id,
            reason_code=rc,
            reason_detail=rd,
        )


class MusicBrainzApiStrategy:
    """Final artist tier — query MusicBrainz. Caches AUTO_MATCHED results to
    local artist catalog so subsequent NormalizationStrategy runs hit locally.

    Preserves the legacy _try_mb_match side effect: on AUTO_MATCHED, upserts
    the MB result into the local Artist catalog.
    """

    _MIN_CANDIDATE_SCORE = 60  # legacy threshold; candidates below this are noise

    def __init__(
        self,
        mb_client: MusicBrainzClientProtocol,
        artist_repo: ArtistCatalogRepository,
        mb_auto_link_score: int = MB_AUTO_LINK_SCORE,
        mb_score_gap: int = MB_SCORE_GAP,
    ) -> None:
        self._mb = mb_client
        self._catalog = artist_repo
        self._high = mb_auto_link_score
        self._gap = mb_score_gap

    def apply(self, broadcast_artist: BroadcastArtist) -> ArtistMatchResult | None:
        results = self._mb.search_artist(broadcast_artist.original_name)
        if not results:
            return None

        candidates: list[MbArtistResult] = [
            r for r in results if r.get("score", 0) >= self._MIN_CANDIDATE_SCORE
        ]
        if not candidates:
            return None

        candidates.sort(key=lambda r: r.get("score", 0), reverse=True)
        best = candidates[0]
        second = candidates[1].get("score", 0) if len(candidates) > 1 else 0
        top_score = float(best.get("score", 0))
        gap = float(best.get("score", 0) - second)

        status, rc, rd = _decide_artist_zone(top_score, gap, self._high, self._gap)

        # Preserve legacy side effect: upsert MB result into local catalog on
        # AUTO_MATCHED so subsequent NormalizationStrategy runs can hit locally.
        if status == MatchStatus.AUTO_MATCHED:
            self._catalog.upsert(Artist(
                id=best["id"],
                name=best["name"],
                sort_name=best.get("sort-name", best["name"]),
                disambiguation=best.get("disambiguation"),
            ))

        return ArtistMatchResult(
            status=status,
            tier=MatchTier.MUSICBRAINZ_API,
            confidence_score=top_score,
            target_id=best["id"],
            reason_code=rc,
            reason_detail=rd,
        )


class _Candidate(NamedTuple):
    artist: Artist
    score: float


def _try_rule_match(
    broadcast_artist: BroadcastArtist,
    rules: list[MappingRule],
    broadcast_artist_repo: BroadcastArtistRepository,
    match_repo: MatchRepository,
) -> bool:
    """Check mapping rules; create match and return True if hit, else False."""
    for rule in rules:
        if rule.target_type == TargetType.ARTIST and rule_matches(
            rule.source_pattern, broadcast_artist.normalized_name
        ):
            broadcast_artist_repo.update_match_status(
                broadcast_artist.id, MatchStatus.AUTO_MATCHED
            )
            match_repo.create(Match(
                id=uuid4(),
                artist_id=broadcast_artist.id,
                target_id=rule.target_id,
                target_type=TargetType.ARTIST,
                confidence_score=100.0,
                match_tier=MatchTier.MANUAL,
            ))
            return True
    return False


def _try_exact_match(
    broadcast_artist: BroadcastArtist,
    all_canonical: list[Artist],
    broadcast_artist_repo: BroadcastArtistRepository,
    match_repo: MatchRepository,
) -> bool:
    """Return True and record match if exact normalized-name hit, else False."""
    for canonical in all_canonical:
        if normalize_artist(canonical.name) == broadcast_artist.normalized_name:
            broadcast_artist_repo.update_match_status(
                broadcast_artist.id, MatchStatus.AUTO_MATCHED
            )
            match_repo.create(Match(
                id=uuid4(),
                artist_id=broadcast_artist.id,
                target_id=canonical.id,
                target_type=TargetType.ARTIST,
                confidence_score=100.0,
                match_tier=MatchTier.NORMALIZATION,
            ))
            return True
    return False


def _try_fuzzy_match(
    broadcast_artist: BroadcastArtist,
    all_canonical: list[Artist],
    broadcast_artist_repo: BroadcastArtistRepository,
    match_repo: MatchRepository,
    mb_auto_link_score: int,
    mb_score_gap: int,
) -> bool:
    """Return True and record match (AUTO or NEEDS_REVIEW) if fuzzy hit, else False."""
    if not all_canonical:
        return False

    candidates: list[_Candidate] = []
    for canonical in all_canonical:
        score = fuzz.token_sort_ratio(
            broadcast_artist.normalized_name,
            normalize_artist(canonical.name),
        )
        if score >= 60:
            candidates.append(_Candidate(artist=canonical, score=score))

    if not candidates:
        return False

    candidates.sort(key=lambda x: x.score, reverse=True)
    top = candidates[0]
    second_score = candidates[1].score if len(candidates) > 1 else 0.0
    confidence_gap = int(top.score) - int(second_score)

    status, tier = _apply_thresholds(
        int(top.score), confidence_gap, mb_auto_link_score, mb_score_gap
    )
    if status is None:
        return False

    broadcast_artist_repo.update_match_status(broadcast_artist.id, status)
    if status == MatchStatus.AUTO_MATCHED:
        match_repo.create(Match(
            id=uuid4(),
            artist_id=broadcast_artist.id,
            target_id=top.artist.id,
            target_type=TargetType.ARTIST,
            confidence_score=int(top.score),
            match_tier=MatchTier.NORMALIZATION,
        ))
    return True


def _try_mb_match(
    broadcast_artist: BroadcastArtist,
    mb_client: MusicBrainzClientProtocol,
    artist_repo: ArtistCatalogRepository,
    broadcast_artist_repo: BroadcastArtistRepository,
    match_repo: MatchRepository,
    mb_auto_link_score: int,
    mb_score_gap: int,
) -> bool:
    """Return True and record match if MB search produces decisive result, else False."""
    mb_results = mb_client.search_artist(broadcast_artist.original_name)
    if not mb_results:
        return False

    mb_candidates: list[MbArtistResult] = [
        result for result in mb_results if result.get("score", 0) >= 60
    ]

    if not mb_candidates:
        return False

    mb_candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
    best_mb_candidate = mb_candidates[0]
    second_mb_score = mb_candidates[1].get("score", 0) if len(mb_candidates) > 1 else 0
    confidence_gap = best_mb_candidate.get("score", 0) - second_mb_score

    mb_status, mb_tier = _apply_thresholds(
        best_mb_candidate.get("score", 0),
        confidence_gap,
        mb_auto_link_score,
        mb_score_gap,
    )
    if mb_status is None:
        return False

    canonical = artist_repo.upsert(Artist(
        id=best_mb_candidate["id"],
        name=best_mb_candidate["name"],
        sort_name=best_mb_candidate.get("sort-name", best_mb_candidate["name"]),
        disambiguation=best_mb_candidate.get("disambiguation"),
    ))
    broadcast_artist_repo.update_match_status(broadcast_artist.id, mb_status)
    if mb_status == MatchStatus.AUTO_MATCHED:
        match_repo.create(Match(
            id=uuid4(),
            artist_id=broadcast_artist.id,
            target_id=canonical.id,
            target_type=TargetType.ARTIST,
            confidence_score=best_mb_candidate.get("score", 0),
            match_tier=MatchTier.MUSICBRAINZ_API,
        ))
    return True


def match_artists_for_playlist(
    playlist_id: UUID,
    broadcast_artist_repo: BroadcastArtistRepository,
    track_identity_repo: BroadcastTrackIdentityRepository,
    artist_repo: ArtistCatalogRepository,
    match_repo: MatchRepository,
    rules_repo: MappingRuleRepository,
    mb_client: MusicBrainzClientProtocol,
    mb_auto_link_score: int = 95,
    mb_score_gap: int = 10,
) -> None:
    """Run artist matching for all PENDING artists linked to this playlist."""
    pending = broadcast_artist_repo.get_pending_for_playlist(playlist_id)
    rules = rules_repo.list_ordered()
    all_canonical = artist_repo.list_all()

    for broadcast_artist in pending:
        if _try_rule_match(broadcast_artist, rules, broadcast_artist_repo, match_repo):
            continue
        if _try_exact_match(broadcast_artist, all_canonical, broadcast_artist_repo, match_repo):
            continue
        if _try_fuzzy_match(
            broadcast_artist, all_canonical, broadcast_artist_repo, match_repo,
            mb_auto_link_score, mb_score_gap,
        ):
            continue
        if _try_mb_match(
            broadcast_artist, mb_client, artist_repo, broadcast_artist_repo, match_repo,
            mb_auto_link_score, mb_score_gap,
        ):
            continue
        broadcast_artist_repo.update_match_status(broadcast_artist.id, MatchStatus.NEEDS_REVIEW)

    # Cascade: AUTO_REJECTED artists → bulk reject child identities
    all_playlist_artists = broadcast_artist_repo.get_all_for_playlist(playlist_id)
    for broadcast_artist in all_playlist_artists:
        if broadcast_artist.match_status == MatchStatus.AUTO_REJECTED:
            track_identity_repo.bulk_reject_by_artist(broadcast_artist.id)


def _apply_thresholds(
    score: int,
    gap: int,
    auto_link_score: int,
    score_gap: int,
) -> tuple[MatchStatus | None, MatchTier | None]:
    """Apply matching thresholds per spec Section 5.2."""
    if score >= auto_link_score and gap >= score_gap:
        return MatchStatus.AUTO_MATCHED, MatchTier.NORMALIZATION
    if score >= auto_link_score and gap < score_gap:
        return MatchStatus.NEEDS_REVIEW, MatchTier.NORMALIZATION
    if score >= 80:
        return MatchStatus.AUTO_MATCHED, MatchTier.NORMALIZATION
    if score >= 60:
        return MatchStatus.NEEDS_REVIEW, MatchTier.NORMALIZATION
    return None, None

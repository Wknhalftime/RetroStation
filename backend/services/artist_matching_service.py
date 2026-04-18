from __future__ import annotations

from typing import NamedTuple
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
from backend.services.matching_utils import rule_matches
from backend.services.mb_client import MusicBrainzClientProtocol
from backend.services.mb_types import MbArtistResult
from backend.services.normalization import normalize_artist

logger = structlog.get_logger()


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

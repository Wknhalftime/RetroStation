from __future__ import annotations

import re
from typing import Any, Protocol
from uuid import UUID, uuid4

import structlog
from rapidfuzz import fuzz

from backend.domain.enums import MatchStatus, MatchTier, TargetType
from backend.domain.models import Artist, Match
from backend.repositories.artists import ArtistRepository
from backend.repositories.global_mapping_rules import GlobalMappingRuleRepository
from backend.repositories.log_artists import LogArtistRepository
from backend.repositories.log_identities import LogIdentityRepository
from backend.repositories.matches import MatchRepository
from backend.services.normalization import normalize_artist

logger = structlog.get_logger()


class MbClientProtocol(Protocol):
    def search_artist(self, name: str) -> list[dict[str, Any]]: ...


def _rule_matches(source_pattern: str, normalized_value: str) -> bool:
    """Check if a global mapping rule matches a normalized value."""
    if source_pattern == normalized_value:
        return True
    try:
        return bool(re.fullmatch(source_pattern, normalized_value))
    except re.error:
        return False


def match_artists_for_playlist(
    playlist_id: UUID,
    log_artist_repo: LogArtistRepository,
    log_identity_repo: LogIdentityRepository,
    artist_repo: ArtistRepository,
    match_repo: MatchRepository,
    rules_repo: GlobalMappingRuleRepository,
    mb_client: MbClientProtocol,
    mb_auto_link_score: int = 95,
    mb_score_gap: int = 10,
) -> None:
    """Run artist matching for all PENDING artists linked to this playlist."""
    pending = log_artist_repo.get_pending_for_playlist(playlist_id)
    rules = rules_repo.list_ordered()

    for log_artist in pending:
        # Pre-check global mapping rules
        rule_matched = False
        for rule in rules:
            if rule.target_type == TargetType.ARTIST and _rule_matches(
                rule.source_pattern, log_artist.normalized_name
            ):
                log_artist_repo.update_match_status(
                    log_artist.id, MatchStatus.AUTO_MATCHED, MatchTier.MANUAL
                )
                match_repo.create(Match(
                    id=uuid4(),
                    artist_id=log_artist.id,
                    target_id=rule.target_id,
                    target_type=TargetType.ARTIST,
                    confidence_score=100.0,
                    match_tier=MatchTier.MANUAL,
                ))
                rule_matched = True
                break
        if rule_matched:
            continue

        # Tier 1: Exact normalized name match against canonical artists
        all_artists = artist_repo.list_all()
        exact_match = None
        for canonical in all_artists:
            if normalize_artist(canonical.name) == log_artist.normalized_name:
                exact_match = canonical
                break

        if exact_match:
            log_artist_repo.update_match_status(
                log_artist.id, MatchStatus.AUTO_MATCHED, MatchTier.NORMALIZATION
            )
            match_repo.create(Match(
                id=uuid4(),
                artist_id=log_artist.id,
                target_id=exact_match.id,
                target_type=TargetType.ARTIST,
                confidence_score=100.0,
                match_tier=MatchTier.NORMALIZATION,
            ))
            continue

        # Tier 2: Fuzzy match via rapidfuzz
        if all_artists:
            candidates: list[dict[str, Any]] = []
            for canonical in all_artists:
                score = fuzz.token_sort_ratio(
                    log_artist.normalized_name,
                    normalize_artist(canonical.name),
                )
                if score >= 60:
                    candidates.append({"artist": canonical, "score": score})

            if candidates:
                candidates.sort(key=lambda x: x["score"], reverse=True)
                top = candidates[0]
                second_score = candidates[1]["score"] if len(candidates) > 1 else 0.0
                gap = top["score"] - second_score

                status, tier = _apply_thresholds(
                    top["score"], gap, mb_auto_link_score, mb_score_gap
                )
                if status is not None:
                    log_artist_repo.update_match_status(log_artist.id, status, tier)
                    if status == MatchStatus.AUTO_MATCHED:
                        match_repo.create(Match(
                            id=uuid4(),
                            artist_id=log_artist.id,
                            target_id=top["artist"].id,
                            target_type=TargetType.ARTIST,
                            confidence_score=top["score"],
                            match_tier=MatchTier.NORMALIZATION,
                        ))
                    elif status == MatchStatus.NEEDS_REVIEW:
                        log_artist_repo.update_match_status(
                            log_artist.id, MatchStatus.NEEDS_REVIEW
                        )
                    continue

        # Tier 3: MusicBrainz API search
        mb_results = mb_client.search_artist(log_artist.original_name)
        if mb_results:
            mb_candidates: list[dict[str, Any]] = []
            for mb_result in mb_results:
                score = mb_result.get("score", 0)
                if score >= 60:
                    mb_candidates.append(mb_result)

            if mb_candidates:
                mb_candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
                top_mb = mb_candidates[0]
                second_mb_score = (
                    mb_candidates[1].get("score", 0) if len(mb_candidates) > 1 else 0.0
                )
                mb_gap = top_mb.get("score", 0) - second_mb_score

                mb_status, mb_tier = _apply_thresholds(
                    top_mb.get("score", 0), mb_gap, mb_auto_link_score, mb_score_gap
                )
                if mb_status is not None:
                    # Upsert canonical artist from MB result
                    canonical = artist_repo.upsert(Artist(
                        id=top_mb["id"],
                        name=top_mb["name"],
                        sort_name=top_mb.get("sort-name", top_mb["name"]),
                        disambiguation=top_mb.get("disambiguation"),
                    ))
                    log_artist_repo.update_match_status(
                        log_artist.id, mb_status,
                        MatchTier.MUSICBRAINZ_API if mb_tier else None,
                    )
                    if mb_status == MatchStatus.AUTO_MATCHED:
                        match_repo.create(Match(
                            id=uuid4(),
                            artist_id=log_artist.id,
                            target_id=canonical.id,
                            target_type=TargetType.ARTIST,
                            confidence_score=top_mb.get("score", 0),
                            match_tier=MatchTier.MUSICBRAINZ_API,
                        ))
                    continue

        # No match from any tier → NEEDS_REVIEW
        log_artist_repo.update_match_status(
            log_artist.id, MatchStatus.NEEDS_REVIEW
        )

    # Cascade: AUTO_REJECTED artists → bulk reject child identities
    # (This handles cases where global rules set AUTO_REJECTED, as well as
    # artists that were already AUTO_REJECTED before this run)
    all_playlist_artists = log_artist_repo.get_all_for_playlist(playlist_id)
    for log_artist in all_playlist_artists:
        if log_artist.match_status == MatchStatus.AUTO_REJECTED:
            log_identity_repo.bulk_reject_by_artist(log_artist.id)


def _apply_thresholds(
    score: float,
    gap: float,
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

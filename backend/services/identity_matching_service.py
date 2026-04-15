from __future__ import annotations

from uuid import UUID, uuid4

import structlog
from rapidfuzz import fuzz

from backend.domain.broadcast import BroadcastTrackIdentity
from backend.domain.enums import MatchStatus, MatchTier, TargetType
from backend.domain.matching import MappingRule, Match
from backend.repositories.broadcast_artists import BroadcastArtistRepository
from backend.repositories.broadcast_track_identities import BroadcastTrackIdentityRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.mapping_rules import MappingRuleRepository
from backend.repositories.matches import MatchRepository
from backend.services.matching_utils import rule_matches
from backend.services.normalization import normalize_title

logger = structlog.get_logger()


def _try_tier0_rule_match(
    identity: BroadcastTrackIdentity,
    rules: list[MappingRule],
    library_file_repo: LibraryFileRepository,
    match_repo: MatchRepository,
    track_identity_repo: BroadcastTrackIdentityRepository,
) -> str | None:
    """Apply mapping rules.

    Returns the matched work_id (or empty string if matched but no work_id exists)
    when a rule hit occurs, or None when no rule matched.
    """
    normalized_sig = identity.normalized_signature
    for rule in rules:
        if rule.target_type != TargetType.LIBRARY_FILE:
            continue
        if not rule_matches(rule.source_pattern, normalized_sig):
            continue
        # Rule hit: look up the library file by target_id (file path or UUID)
        try:
            lib_file = library_file_repo.get_by_id(UUID(rule.target_id))
        except ValueError:
            lib_file = library_file_repo.get_by_path(rule.target_id)
        if lib_file is None:
            continue
        match_repo.create(Match(
            id=uuid4(),
            identity_id=identity.id,
            library_file_id=lib_file.id,
            confidence_score=100.0,
            match_tier=MatchTier.MUSICBRAINZ_ID_EXACT,
        ))
        track_identity_repo.update_match_status(
            identity.id, MatchStatus.AUTO_MATCHED, MatchTier.MUSICBRAINZ_ID_EXACT
        )
        logger.debug(
            "identity_tier0_matched",
            identity_id=str(identity.id),
            rule_id=str(rule.id),
        )
        if lib_file.work_id:
            return lib_file.work_id
        elif lib_file.recording_id:
            return lib_file.recording_id
        return ""
    return None


def _try_tier2_mbid_match(
    identity: BroadcastTrackIdentity,
    broadcast_artist_repo: BroadcastArtistRepository,
    library_file_repo: LibraryFileRepository,
    match_repo: MatchRepository,
    track_identity_repo: BroadcastTrackIdentityRepository,
) -> tuple[MatchStatus, str | None] | None:
    """MBID-graph match.

    Returns a (status, work_id) tuple when a match was recorded (work_id may be
    None when the matched file carries neither work_id nor recording_id), or None
    when no match was found.
    """
    broadcast_artist = broadcast_artist_repo.get_by_id(identity.broadcast_artist_id)
    canonical_artist_mbid: str | None = None
    if broadcast_artist is not None:
        artist_match = match_repo.get_by_artist(broadcast_artist.id)
        if artist_match is not None:
            canonical_artist_mbid = artist_match.target_id

    if not canonical_artist_mbid:
        return None

    candidate_files = library_file_repo.get_by_artist_mbid(canonical_artist_mbid)
    if not candidate_files:
        return None

    normalized_identity_title = normalize_title(identity.original_title)
    best_score = 0.0
    best_file = None
    for lib_file in candidate_files:
        if lib_file.audio.track_title:
            normalized_lib_title = normalize_title(lib_file.audio.track_title)
        else:
            continue
        score = fuzz.ratio(normalized_identity_title, normalized_lib_title)
        if score > best_score:
            best_score = score
            best_file = lib_file

    if best_file is None or best_score < 60:
        return None

    if best_score >= 95:
        status = MatchStatus.AUTO_MATCHED
        tier = MatchTier.MUSICBRAINZ_ID_EXACT
    elif best_score >= 80:
        status = MatchStatus.AUTO_MATCHED
        tier = MatchTier.NORMALIZATION
    else:
        # 60–79 → NEEDS_REVIEW
        status = MatchStatus.NEEDS_REVIEW
        tier = MatchTier.NORMALIZATION

    match_repo.create(Match(
        id=uuid4(),
        identity_id=identity.id,
        library_file_id=best_file.id,
        confidence_score=best_score,
        match_tier=tier,
    ))
    track_identity_repo.update_match_status(identity.id, status, tier)

    work_id: str | None = None
    if status == MatchStatus.AUTO_MATCHED:
        if best_file.work_id:
            work_id = best_file.work_id
        elif best_file.recording_id:
            work_id = best_file.recording_id
        logger.debug(
            "identity_tier2_matched",
            identity_id=str(identity.id),
            score=best_score,
            tier=tier,
        )

    return status, work_id


def match_identities_for_playlist(
    playlist_id: UUID,
    track_identity_repo: BroadcastTrackIdentityRepository,
    broadcast_artist_repo: BroadcastArtistRepository,
    match_repo: MatchRepository,
    library_file_repo: LibraryFileRepository,
    rules_repo: MappingRuleRepository,
) -> list[str]:
    """Run identity matching for all pending identities in this playlist.

    Tiers:
      0 — Global mapping rules (LIBRARY_FILE target_type)
      2 — MBID graph: artist MBID confirmed → rapidfuzz title match
      Fallback — NEEDS_REVIEW / UNKNOWN

    Returns:
        List of work_ids (recording.work_id) for newly AUTO_MATCHED identities,
        for downstream master selection recalculation.
    """
    pending = track_identity_repo.get_pending_for_playlist(playlist_id)

    if not pending:
        logger.info("no_pending_identities", playlist_id=str(playlist_id))
        return []

    rules = rules_repo.list_ordered()

    auto_matched = 0
    needs_review = 0
    work_ids: list[str] = []

    for identity in pending:
        # --- Tier 0: Global mapping rules (LIBRARY_FILE target) ---
        tier0_work_id = _try_tier0_rule_match(
            identity, rules, library_file_repo, match_repo, track_identity_repo
        )
        if tier0_work_id is not None:
            if tier0_work_id:
                work_ids.append(tier0_work_id)
            auto_matched += 1
            continue

        # --- Tier 2: MBID graph ---
        tier2_result = _try_tier2_mbid_match(
            identity, broadcast_artist_repo, library_file_repo, match_repo, track_identity_repo
        )
        if tier2_result is not None:
            tier2_status, tier2_work_id = tier2_result
            if tier2_status == MatchStatus.AUTO_MATCHED:
                auto_matched += 1
                if tier2_work_id:
                    work_ids.append(tier2_work_id)
            else:
                needs_review += 1
            continue

        # --- Fallback: NEEDS_REVIEW / UNKNOWN ---
        track_identity_repo.update_match_status(
            identity.id, MatchStatus.NEEDS_REVIEW, MatchTier.UNCLASSIFIED
        )
        needs_review += 1

    logger.info(
        "identity_matching_complete",
        playlist_id=str(playlist_id),
        auto_matched=auto_matched,
        needs_review=needs_review,
        work_ids_collected=len(work_ids),
    )
    return work_ids

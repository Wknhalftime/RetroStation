from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

import structlog
from rapidfuzz import fuzz
from rapidfuzz.fuzz import token_sort_ratio

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import MatchStatus, MatchTier, TargetType
from backend.domain.library import LibraryFile
from backend.domain.matching import MappingRule, Match
from backend.repositories.broadcast_artists import BroadcastArtistRepository
from backend.repositories.broadcast_track_identities import BroadcastTrackIdentityRepository
from backend.repositories.library_files import LibraryFileRepository
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
from backend.services.matching_utils import normalize_title_for_scoring, rule_matches
from backend.services.mb_client import MusicBrainzClientProtocol
from backend.services.normalization import normalize_title

logger = structlog.get_logger()


@dataclass(frozen=True)
class IdentityMatchResult:
    """Immutable result returned by an IdentityMatchingStrategy.

    Strategies produce values; the service function owns all persistence.
    library_file_id is None ONLY when no candidate exists at all
    (reason_code in {NO_LOCAL_FILES, MISSING_MATCH_RECORD, NO_CANDIDATES}); the
    _score_candidates helper always populates it with best_file.id. triage_bucket
    is NOT on this dataclass — the router computes it from confidence_score.
    """

    status: MatchStatus
    tier: MatchTier
    confidence_score: float
    library_file_id: UUID | None
    work_id: str | None = None
    reason_code: ReasonCode | None = None
    reason_detail: str | None = None


class IdentityMatchingStrategy(Protocol):
    """One resolution tier for a BroadcastTrackIdentity.

    apply() takes both (identity, artist) so strategies do not refetch the
    artist. This differs from ArtistMatchingStrategy.apply(broadcast_artist)
    intentionally; the two Protocols cannot be merged.
    """

    def apply(
        self,
        identity: BroadcastTrackIdentity,
        artist: BroadcastArtist,
    ) -> IdentityMatchResult | None: ...


def _score_candidates(
    broadcast_title: str,
    candidates: list[LibraryFile],
    tier: MatchTier,
    high_threshold: int,
) -> IdentityMatchResult:
    """Score candidates against broadcast_title; return best-match result.

    Normalises both sides with normalize_title_for_scoring() before
    token_sort_ratio. Single-candidate case: gap = 100 (no competition).
    library_file_id is ALWAYS populated with best_file.id — never None.
    Caller must ensure candidates is non-empty.
    """
    assert candidates, "caller must have ensured candidates is non-empty"
    norm_bc = normalize_title_for_scoring(broadcast_title)
    scored: list[tuple[float, LibraryFile]] = sorted(
        (
            (
                float(
                    token_sort_ratio(
                        norm_bc,
                        normalize_title_for_scoring(f.audio.track_title or ""),
                    )
                ),
                f,
            )
            for f in candidates
        ),
        key=lambda x: x[0],
        reverse=True,
    )
    top_score, best = scored[0]
    gap: float = (top_score - scored[1][0]) if len(scored) > 1 else 100.0

    rc: ReasonCode | None
    rd: str | None
    auto_match = (
        top_score >= MB_AUTO_LINK_SCORE
        or (top_score >= high_threshold and gap >= MB_SCORE_GAP)
        or (
            MID_BAND_LOWER <= top_score <= MID_BAND_UPPER
            and gap >= MID_BAND_GAP_THRESHOLD
        )
    )
    if auto_match:
        status, rc, rd = MatchStatus.AUTO_MATCHED, None, None
    elif top_score >= MIN_PRESENTATION_SCORE:
        status = MatchStatus.NEEDS_REVIEW
        if top_score >= high_threshold:
            rc = ReasonCode.AMBIGUOUS_GAP
            rd = format_ambiguous_gap(gap, MB_SCORE_GAP)
        else:
            rc = ReasonCode.LOW_CONFIDENCE
            rd = format_low_confidence(top_score)
    else:
        status = MatchStatus.NEEDS_REVIEW
        rc = ReasonCode.LOW_CONFIDENCE
        rd = format_low_confidence(top_score)

    return IdentityMatchResult(
        status=status,
        tier=tier,
        confidence_score=top_score,
        library_file_id=best.id,
        reason_code=rc,
        reason_detail=rd,
    )


class IdentityMappingRuleStrategy:
    """Tier 0 — global mapping rule override.

    Returns AUTO_MATCHED when a rule's source_pattern matches the identity's
    normalized_signature AND resolves to a real LibraryFile. Rule overrides
    always win over algorithmic matching.
    """

    def __init__(
        self,
        rules: list[MappingRule],
        library_file_repo: LibraryFileRepository,
    ) -> None:
        self._rules = rules
        self._library_file_repo = library_file_repo

    def apply(
        self,
        identity: BroadcastTrackIdentity,
        artist: BroadcastArtist,
    ) -> IdentityMatchResult | None:
        sig = identity.normalized_signature
        for rule in self._rules:
            if rule.target_type != TargetType.LIBRARY_FILE:
                continue
            if not rule_matches(rule.source_pattern, sig):
                continue
            try:
                lib_file = self._library_file_repo.get_by_id(UUID(rule.target_id))
            except ValueError:
                lib_file = self._library_file_repo.get_by_path(rule.target_id)
            if lib_file is None:
                continue
            return IdentityMatchResult(
                status=MatchStatus.AUTO_MATCHED,
                tier=MatchTier.MUSICBRAINZ_ID_EXACT,
                confidence_score=100.0,
                library_file_id=lib_file.id,
                work_id=lib_file.work_id or lib_file.recording_id or "",
            )
        return None


class ResolvedArtistMbidStrategy:
    """Tier 1 — fused MBID fast path.

    Step A: local library lookup by artist MBID (no API call).
    Step B: MB recording search (1 API call) — fires internally when Step A
            is inconclusive (mid-confidence) or finds no local files.

    Gate: artist.match_status in {AUTO_MATCHED, MANUAL_MATCHED}. Once inside
    the gate, apply() ALWAYS returns a non-None result. Returning None for a
    resolved artist would strand the identity because Tier 2 also gates on
    unresolved artists.
    """

    def __init__(
        self,
        library_file_repo: LibraryFileRepository,
        match_repo: MatchRepository,
        mb_client: MusicBrainzClientProtocol,
        high_threshold: int = 80,
    ) -> None:
        self._library_file_repo = library_file_repo
        self._match_repo = match_repo
        self._mb_client = mb_client
        self._high_threshold = high_threshold

    def apply(
        self,
        identity: BroadcastTrackIdentity,
        artist: BroadcastArtist,
    ) -> IdentityMatchResult | None:
        if artist.match_status not in {
            MatchStatus.AUTO_MATCHED,
            MatchStatus.MANUAL_MATCHED,
        }:
            return None

        artist_match = self._match_repo.get_by_artist(artist.id)
        if artist_match is None:
            return IdentityMatchResult(
                status=MatchStatus.NEEDS_REVIEW,
                tier=MatchTier.MUSICBRAINZ_ID_SEARCH,
                confidence_score=0.0,
                library_file_id=None,
                reason_code=ReasonCode.MISSING_MATCH_RECORD,
                reason_detail=(
                    "Artist is resolved but no match record found "
                    "— data inconsistency"
                ),
            )

        mbid = artist_match.target_id
        if mbid is None:
            return IdentityMatchResult(
                status=MatchStatus.NEEDS_REVIEW,
                tier=MatchTier.MUSICBRAINZ_ID_SEARCH,
                confidence_score=0.0,
                library_file_id=None,
                reason_code=ReasonCode.MISSING_MATCH_RECORD,
                reason_detail="Artist match row has no target_id (MBID)",
            )

        # Step A — local library lookup.
        candidate_files = self._library_file_repo.get_by_artist_mbid(mbid)
        if candidate_files:
            local_result = _score_candidates(
                identity.normalized_title,
                candidate_files,
                tier=MatchTier.MUSICBRAINZ_ID_EXACT,
                high_threshold=self._high_threshold,
            )
            if local_result.status == MatchStatus.AUTO_MATCHED:
                return local_result
            # Step B escalation.
            mb_result = self._mb_recording_search(mbid, identity)
            if mb_result is not None:
                return mb_result
            return local_result

        mb_result = self._mb_recording_search(mbid, identity)
        if mb_result is not None:
            return mb_result

        return IdentityMatchResult(
            status=MatchStatus.NEEDS_REVIEW,
            tier=MatchTier.MUSICBRAINZ_ID_SEARCH,
            confidence_score=0.0,
            library_file_id=None,
            reason_code=ReasonCode.NO_LOCAL_FILES,
            reason_detail=(
                "Artist MBID confirmed but no matching local recording found"
            ),
        )

    def _mb_recording_search(
        self,
        mbid: str,
        identity: BroadcastTrackIdentity,
    ) -> IdentityMatchResult | None:
        """Return a scored result if any MB recording maps to a local file.

        Returns None otherwise; caller converts None into a concrete blocked
        result.
        """
        recordings = self._mb_client.search_recording(
            artist_mbid=mbid, title=identity.normalized_title
        )
        for rec in recordings:
            rec_id = rec.get("id")
            if rec_id is None:
                continue
            lib_file = self._library_file_repo.get_by_recording_mbid(rec_id)
            if lib_file is not None:
                return _score_candidates(
                    identity.normalized_title,
                    [lib_file],
                    tier=MatchTier.MUSICBRAINZ_ID_SEARCH,
                    high_threshold=self._high_threshold,
                )
        return None


class BroadcastToLocalStrategy:
    """Tier 2 — fuzzy fallback when artist MBID is not yet confirmed.

    Never fires for resolved artists — Tier 1 always returns a concrete result
    for resolved artists, so the engine never reaches Tier 2 in that case.
    The gate below is a defensive guard.
    """

    def __init__(
        self,
        library_file_repo: LibraryFileRepository,
        high_threshold: int = 80,
    ) -> None:
        self._library_file_repo = library_file_repo
        self._high_threshold = high_threshold

    def apply(
        self,
        identity: BroadcastTrackIdentity,
        artist: BroadcastArtist,
    ) -> IdentityMatchResult | None:
        if artist.match_status in {
            MatchStatus.AUTO_MATCHED,
            MatchStatus.MANUAL_MATCHED,
        }:
            return None

        candidate_files = self._library_file_repo.search_by_artist_name(
            artist.normalized_name
        )
        if not candidate_files:
            return IdentityMatchResult(
                status=MatchStatus.NEEDS_REVIEW,
                tier=MatchTier.LOCAL_FILE_FUZZY,
                confidence_score=0.0,
                library_file_id=None,
                reason_code=ReasonCode.NO_CANDIDATES,
                reason_detail=(
                    f"No library files found for artist "
                    f"'{artist.normalized_name}'"
                ),
            )
        return _score_candidates(
            identity.normalized_title,
            candidate_files,
            tier=MatchTier.LOCAL_FILE_FUZZY,
            high_threshold=self._high_threshold,
        )


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

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

import structlog
from rapidfuzz.fuzz import token_sort_ratio

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import MatchStatus, MatchTier, ReasonCode, TargetType
from backend.domain.library import LibraryFile
from backend.domain.matching import MappingRule, Match
from backend.repositories.artist_catalog import ArtistCatalogRepository
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
)
from backend.services.matching_reasons import (
    format_ambiguous_gap,
    format_low_confidence,
)
from backend.services.matching_utils import normalize_title_for_scoring, rule_matches
from backend.services.mb_client import MusicBrainzClientProtocol

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
    strong_match_threshold: int,
) -> IdentityMatchResult:
    """Score candidates against broadcast_title; return best-match result.

    Normalises both sides with normalize_title_for_scoring() before
    token_sort_ratio. Single-candidate case: gap = 100 (no competition).
    library_file_id is ALWAYS populated with best_file.id — never None.
    work_id is populated from best_file (work_id or recording_id) so every
    strategy returning from this helper exposes a downstream master-selection
    handle without remembering to wire it in. Ties on score break by
    library_file id for determinism across runs. Caller must ensure
    candidates is non-empty.
    """
    if not candidates:
        # `assert` is stripped under `python -O`; use a real guard so an
        # upstream bug surfaces as a ValueError, not a later IndexError.
        raise ValueError("_score_candidates requires at least one candidate")
    norm_bc = normalize_title_for_scoring(broadcast_title)
    # Sort by score DESC, then by library_file id ASC so duplicate-score ties
    # are resolved deterministically. Without the tie-breaker, input order
    # (often non-deterministic from SQL) would decide the match.
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
        key=lambda x: (-x[0], str(x[1].id)),
    )
    top_score, best = scored[0]
    has_competitor = len(scored) > 1
    gap: float = (top_score - scored[1][0]) if has_competitor else 100.0

    rc: ReasonCode | None
    rd: str | None
    # Both gap-dependent auto-match clauses require `has_competitor`: when
    # there is no real second candidate, gap is synthesized to 100 and a
    # lone result at score 85 would otherwise auto-match via the
    # strong_match_threshold+gap path — exactly the "too permissive" failure the
    # mid-band guard prevents. The MB_AUTO_LINK_SCORE (>= 95) path is
    # unguarded because at that confidence a token match is effectively a
    # literal identity and a lone result is still trustworthy.
    auto_match = (
        top_score >= MB_AUTO_LINK_SCORE
        or (
            has_competitor
            and top_score >= strong_match_threshold
            and gap >= MB_SCORE_GAP
        )
        or (
            has_competitor
            and MID_BAND_LOWER <= top_score <= MID_BAND_UPPER
            and gap >= MID_BAND_GAP_THRESHOLD
        )
    )
    if auto_match:
        status, rc, rd = MatchStatus.AUTO_MATCHED, None, None
    elif has_competitor and top_score >= strong_match_threshold:
        # AMBIGUOUS_GAP only applies when a real peer is close on score.
        # Lone candidates (synthetic gap=100) fall through to LOW_CONFIDENCE
        # so the "within 100 points" message never renders.
        status = MatchStatus.NEEDS_REVIEW
        rc = ReasonCode.AMBIGUOUS_GAP
        rd = format_ambiguous_gap(gap, MB_SCORE_GAP)
    else:
        status = MatchStatus.NEEDS_REVIEW
        rc = ReasonCode.LOW_CONFIDENCE
        rd = format_low_confidence(top_score)

    return IdentityMatchResult(
        status=status,
        tier=tier,
        confidence_score=top_score,
        library_file_id=best.id,
        work_id=best.work_id or best.recording_id or "",
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

    Match.target_id may be a real MusicBrainz MBID (most strategies) or a
    local catalog UUID (NormalizationStrategy exact-match on local-only
    canonicals). The strategy resolves the authoritative real_mbid first by
    looking up target_id in the catalog; if found, it reads .mbid from the
    catalog artist rather than trusting the UUID value.

    Step A: local library lookup by real_mbid (no API call).
    Step B: MB recording search (1 API call) — fires only when real_mbid is
            not None. Naturally skipped for true local-only artists.
    Step C: name-based fuzzy fallback — fires when real_mbid is None (local-
            only artist with no MB link yet), or when A and B both yield
            nothing. Uses original_name (not normalized_name) to preserve
            punctuation for the substring search.

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
        catalog_repo: ArtistCatalogRepository,
        strong_match_threshold: int = 80,
    ) -> None:
        self._library_file_repo = library_file_repo
        self._match_repo = match_repo
        self._mb_client = mb_client
        self._catalog_repo = catalog_repo
        self._strong_match_threshold = strong_match_threshold

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

        mbid = (artist_match.target_id or "").strip()
        if not mbid:
            # Guard against None AND empty/whitespace-only target_id — data
            # inconsistency could yield "" which `is None` misses, and an
            # empty MBID would poison both the local lookup and MB query.
            return IdentityMatchResult(
                status=MatchStatus.NEEDS_REVIEW,
                tier=MatchTier.MUSICBRAINZ_ID_SEARCH,
                confidence_score=0.0,
                library_file_id=None,
                reason_code=ReasonCode.MISSING_MATCH_RECORD,
                reason_detail="Artist match row has no valid target_id (MBID)",
            )

        # Resolve target_id → real MusicBrainz MBID.
        # target_id may be a local catalog UUID (NormalizationStrategy exact-
        # match on local-only canonicals) or a real MBID (all other strategies).
        # Look up by catalog primary key first:
        #   • Found     → target_id is a local UUID; read catalog_artist.mbid.
        #                 This uses the authoritative field rather than trusting
        #                 the UUID value, and naturally handles artists that gain
        #                 an MB link after the initial match.
        #   • Not found → target_id is already the MBID; use it directly.
        catalog_artist = self._catalog_repo.get_by_id(mbid)
        real_mbid: str | None = (
            catalog_artist.mbid if catalog_artist is not None else mbid
        )

        if real_mbid:
            # Step A — local library lookup by artist MBID.
            candidate_files = self._library_file_repo.get_by_artist_mbid(real_mbid)
            if candidate_files:
                local_result = _score_candidates(
                    identity.normalized_title,
                    candidate_files,
                    tier=MatchTier.MUSICBRAINZ_ID_EXACT,
                    strong_match_threshold=self._strong_match_threshold,
                )
                if local_result.status == MatchStatus.AUTO_MATCHED:
                    return local_result
                # Step B escalation: try MB recording search for a better match.
                mb_result = self._mb_recording_search(real_mbid, identity)
                if mb_result is not None:
                    return mb_result
                return local_result

            # Step B — MB recording search when no local files found by MBID.
            mb_result = self._mb_recording_search(real_mbid, identity)
            if mb_result is not None:
                return mb_result

        # Step C — name-based fallback.
        # Fires when real_mbid is None (local-only artist with no MB link yet),
        # or when both A and B yield nothing.
        # Uses original_name so the DB LOWER(TRIM(artist_name)) LIKE %needle%
        # search preserves punctuation — e.g. "AC/DC" → "ac/dc" correctly
        # matches library files stored as "AC/DC", whereas normalized_name
        # "ac dc" would not.
        name_candidates = self._library_file_repo.search_by_artist_name(
            artist.original_name
        )
        if name_candidates:
            return _score_candidates(
                identity.normalized_title,
                name_candidates,
                tier=MatchTier.LOCAL_FILE_FUZZY,
                strong_match_threshold=self._strong_match_threshold,
            )

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
        """Return the best-scored local file across ALL matching MB recordings.

        Collects candidates from every MB recording that resolves to a local
        file, then scores them together — so a later recording that scores
        better than an earlier one is still chosen. Returns None only when
        no MB recording maps to any local file.
        """
        recordings = self._mb_client.search_recording(
            artist_mbid=mbid, title=identity.normalized_title
        )
        # Collect ALL local files across recordings before scoring.
        # Deduplicate by library-file id since the same file can legitimately
        # appear under multiple recording MBIDs (e.g., merged recordings).
        seen: set[UUID] = set()
        all_candidates: list[LibraryFile] = []
        for rec in recordings:
            rec_id = rec.get("id")
            if rec_id is None:
                continue
            for lib_file in self._library_file_repo.get_by_recording_mbid(rec_id):
                if lib_file.id in seen:
                    continue
                seen.add(lib_file.id)
                all_candidates.append(lib_file)
        if not all_candidates:
            return None
        return _score_candidates(
            identity.normalized_title,
            all_candidates,
            tier=MatchTier.MUSICBRAINZ_ID_SEARCH,
            strong_match_threshold=self._strong_match_threshold,
        )


class BroadcastToLocalStrategy:
    """Tier 2 — fuzzy fallback when artist MBID is not yet confirmed.

    Never fires for resolved artists — Tier 1 always returns a concrete result
    for resolved artists, so the engine never reaches Tier 2 in that case.
    The gate below is a defensive guard.
    """

    def __init__(
        self,
        library_file_repo: LibraryFileRepository,
        strong_match_threshold: int = 80,
    ) -> None:
        self._library_file_repo = library_file_repo
        self._strong_match_threshold = strong_match_threshold

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
            artist.original_name
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
                    f"'{artist.original_name}'"
                ),
            )
        return _score_candidates(
            identity.normalized_title,
            candidate_files,
            tier=MatchTier.LOCAL_FILE_FUZZY,
            strong_match_threshold=self._strong_match_threshold,
        )


class IdentityMatchingEngine:
    """Iterates strategies in order; returns first non-None result.

    No persistence — the service function (match_identities_for_playlist)
    owns all DB writes.
    """

    def __init__(self, strategies: list[IdentityMatchingStrategy]) -> None:
        self._strategies = strategies

    def resolve(
        self,
        identity: BroadcastTrackIdentity,
        artist: BroadcastArtist,
    ) -> IdentityMatchResult | None:
        for strategy in self._strategies:
            result = strategy.apply(identity, artist)
            if result is not None:
                return result
        return None


def match_identities_for_playlist(
    playlist_id: UUID,
    track_identity_repo: BroadcastTrackIdentityRepository,
    broadcast_artist_repo: BroadcastArtistRepository,
    match_repo: MatchRepository,
    library_file_repo: LibraryFileRepository,
    rules_repo: MappingRuleRepository,
    mb_client: MusicBrainzClientProtocol,
    catalog_repo: ArtistCatalogRepository,
    strong_match_threshold: int = 80,
) -> list[str]:
    """Resolve pending identities for a playlist.

    Strategy order: mapping rules → MBID fast path (with internal MB recording
    search) → fuzzy fallback. Persistence lives here, not in strategies.

    Returns list of work_ids for newly AUTO_MATCHED identities so the caller
    can trigger downstream master-selection recalculation. This return
    contract is preserved from the legacy implementation.
    """
    pending = track_identity_repo.get_pending_for_playlist(playlist_id)
    if not pending:
        logger.info("no_pending_identities", playlist_id=str(playlist_id))
        return []

    rules = rules_repo.list_ordered()

    # Resolve artists up-front to avoid N+1 queries inside the loop.
    artist_ids = [identity.broadcast_artist_id for identity in pending]
    artists_by_id = {
        a.id: a for a in broadcast_artist_repo.get_by_ids(artist_ids)
    }

    engine = IdentityMatchingEngine([
        IdentityMappingRuleStrategy(rules, library_file_repo),
        ResolvedArtistMbidStrategy(
            library_file_repo, match_repo, mb_client, catalog_repo,
            strong_match_threshold,
        ),
        BroadcastToLocalStrategy(library_file_repo, strong_match_threshold),
    ])

    auto_matched = 0
    needs_review = 0
    work_ids: list[str] = []

    for identity in pending:
        artist = artists_by_id.get(identity.broadcast_artist_id)
        if artist is None:
            # Persist a terminal NEEDS_REVIEW so the orphan surfaces in review
            # tooling and stops getting reprocessed on every matching run.
            logger.warning(
                "orphaned_identity_marked_for_review",
                identity_id=str(identity.id),
                missing_artist_id=str(identity.broadcast_artist_id),
            )
            track_identity_repo.update_match_status(
                identity.id,
                MatchStatus.NEEDS_REVIEW,
                MatchTier.UNCLASSIFIED,
                reason_code=ReasonCode.ORPHANED_IDENTITY,
                reason_detail=(
                    f"No broadcast_artist row for id "
                    f"{identity.broadcast_artist_id}"
                ),
            )
            needs_review += 1
            continue

        result = engine.resolve(identity, artist)

        if result is None:
            # Engine exhausted all strategies. Should not happen given the
            # strategies' defensive exhaustiveness, but persist NEEDS_REVIEW so
            # the identity is surfaced rather than stuck PENDING.
            track_identity_repo.update_match_status(
                identity.id,
                MatchStatus.NEEDS_REVIEW,
                MatchTier.UNCLASSIFIED,
                reason_code=ReasonCode.NO_CANDIDATES,
                reason_detail=(
                    "All matching strategies exhausted — no result produced"
                ),
            )
            needs_review += 1
            continue

        track_identity_repo.update_match_status(
            identity.id,
            result.status,
            result.tier,
            reason_code=result.reason_code,
            reason_detail=result.reason_detail,
        )

        # Persist a matches row whenever a best candidate was scored — including
        # NEEDS_REVIEW — so the resolution-center UI's LEFT JOIN matches surfaces
        # the real confidence_score/triage_bucket instead of NULL/"blocked".
        if result.library_file_id is not None:
            match_repo.create(Match(
                id=uuid4(),
                identity_id=identity.id,
                library_file_id=result.library_file_id,
                confidence_score=result.confidence_score,
                match_tier=result.tier,
            ))

        if result.status == MatchStatus.AUTO_MATCHED:
            auto_matched += 1
            if result.work_id:
                work_ids.append(result.work_id)
        else:
            needs_review += 1

    logger.info(
        "identity_matching_complete",
        playlist_id=str(playlist_id),
        auto_matched=auto_matched,
        needs_review=needs_review,
        work_ids_collected=len(work_ids),
    )
    return work_ids

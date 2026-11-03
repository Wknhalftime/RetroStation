from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID

import httpx
import structlog
from rapidfuzz import fuzz

from backend.domain.broadcast import BroadcastArtist
from backend.domain.catalog import Artist
from backend.domain.enums import MatchStatus, MatchTier, ReasonCode, TargetType
from backend.domain.log_name_status import NameState
from backend.domain.matching import MappingRule
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.broadcast_artists import BroadcastArtistRepository
from backend.repositories.broadcast_track_identities import BroadcastTrackIdentityRepository
from backend.repositories.log_name_links import LogNameLinkRepository
from backend.repositories.log_name_placeholders import LogNamePlaceholderRepository
from backend.repositories.mapping_rules import MappingRuleRepository
from backend.repositories.matches import MatchRepository
from backend.services.log_name_link_service import (
    NO_LINK_STORE,
    AnswerPlan,
    LinkTarget,
    LogNameLinkService,
    MatcherAnswer,
)
from backend.services.matching_constants import (
    MAX_ARTIST_SUGGESTIONS,
    MB_AUTO_LINK_SCORE,
    MB_MIN_CANDIDATE_SCORE,
    MB_SCORE_GAP,
    MIN_PRESENTATION_SCORE,
)
from backend.services.matching_progress import NO_PROGRESS, MatchingProgress, ticking
from backend.services.matching_reasons import (
    format_answer_suggested,
    format_deferred_retry,
    format_link_rejected,
    format_listed_name,
    format_placeholder_refused,
)
from backend.services.matching_utils import is_likely_truncated, rule_matches
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
class ArtistSuggestion:
    """An artist a result suggests (phase 1c S4, C35): its target (an MBID, or a local
    artist's catalog id), its score, and the MusicBrainz hit when the MusicBrainz tier
    found it (the catalog takes it before its link)."""

    target_id: str
    score: float
    mb_candidate: MbArtistResult | None = None


@dataclass(frozen=True)
class ArtistMatchResult:
    """Immutable result returned by an ArtistMatchingStrategy.

    target_id for AUTO_MATCHED results is either:
    - the artist MBID (MusicBrainzApiStrategy, MappingRuleStrategy, and
      NormalizationStrategy against MBID-bearing canonicals), or
    - the local catalog UUID (Artist.id) for NormalizationStrategy exact matches
      against local-only canonicals (mbid=None). In that case the identity tier's
      ResolvedArtistMbidStrategy falls back to name-based search (Step C) when
      get_by_artist_mbid finds nothing.

    AUTO_MATCHED requires a good match score — an MBID is NOT required.

    reason_code / reason_detail are None for AUTO_MATCHED; populated for
    NEEDS_REVIEW using the stable ReasonCode vocabulary.

    suggestions (phase 1c S4) are what a NEEDS_REVIEW / LINK_SUGGESTED result suggests,
    best first, at most MAX_ARTIST_SUGGESTIONS, each at the presentation floor or over;
    empty for every other result. target_id and confidence_score are the first one's.

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
    suggestions: tuple[ArtistSuggestion, ...] = ()


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
    """The zone of a fuzzy or MusicBrainz top score, shared by NormalizationStrategy and
    MusicBrainzApiStrategy.

    From artist model phase 1c (C35, AUD-R038; SPEC S4) only the unconditional gate links: a
    score of ``mb_auto_link_score`` (operator-tunable, MB_AUTO_LINK_SCORE by default) or more,
    gap irrelevant, no competitor needed (at 95+ the token match is effectively a literal
    identity). Every other score is a suggestion: NEEDS_REVIEW / LINK_SUGGESTED, which starts
    no cascade. That covers the two lead rules this zone had (``strong_match_threshold`` with a
    ``score_gap`` lead over a real competitor; 55 to 64 with a 5-point lead), through which
    named stubs were matched wrongly (E7), and what was AMBIGUOUS_GAP or LOW_CONFIDENCE. The
    lead arguments stay in the signature, unused, for its callers until phase 4's linker.
    """
    if top_score >= mb_auto_link_score:
        return MatchStatus.AUTO_MATCHED, None, None
    return (
        MatchStatus.NEEDS_REVIEW,
        ReasonCode.LINK_SUGGESTED,
        format_answer_suggested(top_score),
    )


def _suggestions(
    scored: list[tuple[float, str, MbArtistResult | None]], floor: float
) -> tuple[ArtistSuggestion, ...]:
    """The best MAX_ARTIST_SUGGESTIONS of *scored* (already best first) at *floor* or over."""
    return tuple(
        ArtistSuggestion(target_id=target, score=score, mb_candidate=hit)
        for score, target, hit in scored[:MAX_ARTIST_SUGGESTIONS]
        if score >= floor
    )


def _exact_answer(targets: list[str], tier: MatchTier) -> ArtistMatchResult:
    """An exact name (100): one artist links; several are suggested, in target order (C35)."""
    if len(targets) == 1:
        return ArtistMatchResult(
            status=MatchStatus.AUTO_MATCHED,
            tier=tier,
            confidence_score=100.0,
            target_id=targets[0],
        )
    suggested = _suggestions([(100.0, target, None) for target in sorted(targets)], 100.0)
    return ArtistMatchResult(
        status=MatchStatus.NEEDS_REVIEW,
        tier=tier,
        confidence_score=100.0,
        target_id=suggested[0].target_id,
        reason_code=ReasonCode.LINK_SUGGESTED,
        reason_detail=format_answer_suggested(100.0),
        suggestions=suggested,
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
        # Exact pass 1 — MBID-bearing canonicals: target_id is the MBID so the identity tier's
        # MBID graph path works with no further changes. One artist links; several of the name
        # (two acts both called Nirvana) are suggested (phase 1c S4, C35). The catalog leaves
        # a second artist of a name without a normalized name, so the name itself is compared.
        exact = [
            c.mbid
            for c in self._all_canonical
            if c.mbid is not None and normalize_artist(c.name) == target
        ]
        if exact:
            return _exact_answer(exact, MatchTier.NORMALIZATION)

        # Exact pass 2 — local-only canonicals (mbid=None). An exact name (100%) qualifies; an
        # MBID is not required. target_id is the local catalog UUID; the identity tier's
        # ResolvedArtistMbidStrategy falls back to name-based search (Step C) when
        # get_by_artist_mbid returns no files. An MBID artist of the name wins over a local one
        # (pass 1): the local one is the library's own copy of the act.
        local = [
            c.id
            for c in self._all_canonical
            if c.mbid is None and normalize_artist(c.name) == target
        ]
        if local:
            # Several local-only artists of the name link the first, as before 1c: the
            # ruling's several artists are several MBID artists (Lance, 2026-11-02).
            return _exact_answer(local[:1], MatchTier.NORMALIZATION)

        # Fuzzy pass — restrict to MBID-bearing canonicals, without the artists the local-artist
        # linker gave an MBID (AUD-R026). This filter is what keeps artist matching unchanged by
        # D15 (Lance, 2026-10-08): a linked artist never joins the fuzzy pool, even when release
        # enrichment later confirms its MBID. It still matches by exact name, above. If none
        # exist, fall through so MusicBrainzApiStrategy can try.
        with_mbid = [
            c for c in self._all_canonical if c.mbid is not None and not c.linked_by_lookup
        ]
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
            top_score,
            gap,
            self._strong_match_threshold,
            self._gap,
            has_competitor,
            mb_auto_link_score=self._mb_auto_link_score,
        )
        assert best.mbid is not None  # guaranteed by the with_mbid filter above
        suggestions = (
            ()
            if status == MatchStatus.AUTO_MATCHED
            else _suggestions(
                [(score, c.mbid or "", None) for score, c in scored],
                self._min_presentation_score,
            )
        )
        return ArtistMatchResult(
            status=status,
            tier=MatchTier.NORMALIZATION,
            confidence_score=top_score,
            target_id=best.mbid,
            reason_code=rc,
            reason_detail=rd,
            suggestions=suggestions,
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
            top_score,
            gap,
            self._strong_match_threshold,
            self._gap,
            has_competitor,
            mb_auto_link_score=self._mb_auto_link_score,
        )

        suggestions = (
            ()
            if status == MatchStatus.AUTO_MATCHED
            else _suggestions(
                [(float(r.get("score", 0)), r["id"], r) for r in candidates],
                MB_MIN_CANDIDATE_SCORE,
            )
        )
        return ArtistMatchResult(
            status=status,
            tier=MatchTier.MUSICBRAINZ_API,
            confidence_score=top_score,
            target_id=best["id"],
            reason_code=rc,
            reason_detail=rd,
            mb_candidate=best,
            suggestions=suggestions,
        )


class TruncatedNameMbStrategy:
    """Gates an inner strategy by the truncation heuristic.

    Returns the inner strategy's result when ``original_name`` looks truncated
    by the broadcast system's fixed-width field, else None so the engine
    falls through to the next tier (DeferredRetryStrategy in production
    wiring).

    Single responsibility: routing. Match logic stays in the inner strategy.
    """

    def __init__(self, inner: ArtistMatchingStrategy, max_len: int) -> None:
        self._inner = inner
        self._max_len = max_len

    def apply(self, broadcast_artist: BroadcastArtist) -> ArtistMatchResult | None:
        if not is_likely_truncated(broadcast_artist.original_name, self._max_len):
            return None
        return self._inner.apply(broadcast_artist)


class DeferredRetryStrategy:
    """Terminal strategy — always returns NEEDS_REVIEW/DEFERRED_RETRY.

    Reached only when every higher tier returned None. Eliminates the
    ``result is None → NO_CANDIDATES`` branch in the orchestration result
    loop: every artist gets a terminal result. The reset paths in the
    artist_matching_task pick these up on the next ingestion so previously-
    deferred artists get another local pass after the catalog has grown.
    """

    def apply(self, broadcast_artist: BroadcastArtist) -> ArtistMatchResult | None:
        return ArtistMatchResult(
            status=MatchStatus.NEEDS_REVIEW,
            tier=MatchTier.UNCLASSIFIED,
            confidence_score=0.0,
            target_id="",
            reason_code=ReasonCode.DEFERRED_RETRY,
            reason_detail=format_deferred_retry(),
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


@dataclass(frozen=True)
class ArtistMatchThresholds:
    """Score thresholds and limits shared by every phase of artist matching.

    Threaded through match_artists_for_playlist and the engine builders
    (ArtistEngineInputs) as one object instead of five loose ints — see
    AUD-040. Defaults are identical to the loose parameter defaults it replaced.
    """

    strong_match_threshold: int = 80
    mb_score_gap: int = MB_SCORE_GAP
    mb_auto_link_score: int = MB_AUTO_LINK_SCORE
    min_presentation_score: int = MIN_PRESENTATION_SCORE
    broadcast_name_max_len: int = 30


# Frozen singleton so match_artists_for_playlist's default arg is not a
# function call (ruff B008) — safe because ArtistMatchThresholds is immutable.
_DEFAULT_ARTIST_MATCH_THRESHOLDS = ArtistMatchThresholds()


def _run_local_phase(
    pending: list[BroadcastArtist],
    engine: ArtistMatchingEngine,
) -> tuple[dict[UUID, ArtistMatchResult], list[BroadcastArtist]]:
    """Phase 1: the local engine (build_local_artist_engine) — runs for every artist.

    Returns (resolved_results, unresolved_artists). Unresolved artists fall
    through to Phase 2 (truncated-MB + DeferredRetryStrategy fallback).
    """
    resolved: dict[UUID, ArtistMatchResult] = {}
    unresolved: list[BroadcastArtist] = []
    for broadcast_artist in pending:
        result = engine.resolve(broadcast_artist)
        if result is not None:
            resolved[broadcast_artist.id] = result
        else:
            unresolved.append(broadcast_artist)
    return resolved, unresolved


def _build_truncated_search_map(
    unresolved: list[BroadcastArtist],
    mb_client: MusicBrainzClientProtocol,
    broadcast_name_max_len: int,
) -> tuple[dict[str, list[MbArtistResult]], int]:
    """Filter unresolved to truncated-only, then coalesce MB searches.

    Non-truncated names never trigger an MB search (Invariant 3). Wraps
    ``coalesce_artist_searches`` with the truncation pre-filter — the
    coalescer's existing httpx error-omit-key semantics are preserved but
    now apply only to the truncated subset, not the full pending list.

    NOTE: there are TWO truncation checks in Phase 2 — this pre-filter and
    the gate inside ``TruncatedNameMbStrategy``. They have different jobs:

    - This pre-filter is for *efficiency*: only truncated names are added
      to the search map, so non-truncated names never trigger a coalesced
      MB search call in the first place.
    - ``TruncatedNameMbStrategy`` is for *correctness*: it stops
      ``MusicBrainzApiStrategy`` from falling back to a live ``search_artist``
      call for any unresolved name absent from the map. Without that gate,
      non-truncated names would still hit MB at per-row resolve time.

    Both must remain. Removing only one silently re-enables unconstrained
    MB calls for non-truncated names.
    """
    truncated = [
        a for a in unresolved if is_likely_truncated(a.original_name, broadcast_name_max_len)
    ]
    if not truncated:
        return {}, 0
    return coalesce_artist_searches(truncated, mb_client)


def _run_mb_or_deferred_phase(
    unresolved: list[BroadcastArtist],
    engine: ArtistMatchingEngine,
) -> dict[UUID, ArtistMatchResult]:
    """Phase 2: the engine build_mb_artist_engine builds (TruncatedNameMbStrategy →
    DeferredRetryStrategy).

    DeferredRetryStrategy is terminal — every artist gets a result
    (Invariant 2 — no orphaned ``result is None`` state; resolve_one_name enforces it).
    """
    return {
        broadcast_artist.id: resolve_one_name(broadcast_artist, engine)
        for broadcast_artist in unresolved
    }


def listed_name_result() -> ArtistMatchResult:
    """The result for a log name on the not-an-artist list (C7, phase 1c S6, E3): no engine
    runs for it and nothing is linked; it waits in review with NO_CANDIDATES, which starts no
    cascade. The task and the gate's dry run both give it."""
    return ArtistMatchResult(
        status=MatchStatus.NEEDS_REVIEW,
        tier=MatchTier.UNCLASSIFIED,
        confidence_score=0.0,
        target_id="",
        reason_code=ReasonCode.NO_CANDIDATES,
        reason_detail=format_listed_name(),
    )


def hold_back_refused_link(
    broadcast_artist: BroadcastArtist, result: ArtistMatchResult, links: LogNameLinkService | None
) -> ArtistMatchResult:
    """The result to write for *broadcast_artist* (SPEC 1b S4, 1c S5, E3; C7, C19).

    A link the writer refuses is held back, in this order:

    - to one of MusicBrainz's special-purpose artists (1c S5): an automatic match to one sends
      the name to review with NO_CANDIDATES;
    - to an artist whose link the curator rejected (1b S4): LINK_REJECTED.

    Neither writes a link or a catalog upsert, or starts a cascade (only AUTO_REJECTED and
    DEFERRED_RETRY do). A suggestion of such an artist is dropped; when every suggestion is
    dropped, the result is held back the same way. Every other result is returned as it is.
    The task and the gate's dry run both ask this, so a dry run decides what the task would.
    """
    if links is None or (result.status != MatchStatus.AUTO_MATCHED and not result.suggestions):
        return result
    placeholders = links.placeholders_in(matcher_answer(result))

    def placeholder(target: str) -> tuple[ReasonCode, str]:
        return ReasonCode.NO_CANDIDATES, format_placeholder_refused(placeholders[target])

    if placeholders:
        result = _without(result, set(placeholders), placeholder)
        if result.status != MatchStatus.AUTO_MATCHED and not result.suggestions:
            return result
    rejected = links.rejected_in(broadcast_artist.id, matcher_answer(result))

    def rejected_pair(target: str) -> tuple[ReasonCode, str]:
        return ReasonCode.LINK_REJECTED, format_link_rejected(rejected[target].name)

    return _without(result, set(rejected), rejected_pair) if rejected else result


def _without(
    result: ArtistMatchResult,
    refused: set[str],
    reason: Callable[[str], tuple[ReasonCode, str]],
) -> ArtistMatchResult:
    """*result* without the targets in *refused*: an automatic match to one, or suggestions
    that were all refused, are held back with the reason the first refused target gives."""
    if result.status == MatchStatus.AUTO_MATCHED:
        if result.target_id not in refused:
            return result
        return _held_back(result, *reason(result.target_id))
    kept = tuple(s for s in result.suggestions if s.target_id not in refused)
    if not kept:
        return _held_back(result, *reason(result.suggestions[0].target_id))
    if len(kept) == len(result.suggestions):
        return result
    return replace(
        result,
        target_id=kept[0].target_id,
        confidence_score=kept[0].score,
        mb_candidate=kept[0].mb_candidate,
        suggestions=kept,
    )


def _held_back(result: ArtistMatchResult, code: ReasonCode, detail: str) -> ArtistMatchResult:
    return replace(
        result,
        status=MatchStatus.NEEDS_REVIEW,
        reason_code=code,
        reason_detail=detail,
        mb_candidate=None,
        suggestions=(),
    )


def matcher_answer(result: ArtistMatchResult) -> MatcherAnswer:
    """What *result* writes, for the link service: the status it names, and its link (an
    automatic match) or its suggestions (phase 1c S3, S4)."""
    proposed = NameState(result.status, result.reason_code, result.reason_detail)
    if result.status == MatchStatus.AUTO_MATCHED:
        link = LinkTarget(
            result.target_id, result.tier, result.confidence_score, result.mb_candidate
        )
        return MatcherAnswer(proposed, link=link)
    return MatcherAnswer(
        proposed,
        suggestions=tuple(
            LinkTarget(s.target_id, result.tier, s.score, s.mb_candidate)
            for s in result.suggestions
        ),
    )


def _persist_artist_result(
    broadcast_artist: BroadcastArtist, plan: AnswerPlan, links: LogNameLinkService
) -> bool:
    """Apply a single result, planned by the link service (``plan_answer``). Owned by
    orchestration so strategies stay read-only (Invariant 1); the writes are the link
    service's (phase 1c S3).

    Returns whether the result was written. The name's status, from its links, is written
    first, guarded on the PENDING status this run read (AUD-R018): if the user decided the
    artist in the meantime, their decision wins and nothing that follows from this result
    (catalog upsert, link, suggestion) is written either. Then a MusicBrainz hit goes into the
    catalog before its link, so the link names the artist (phase 1a S6).
    """
    written = links.write_plan(broadcast_artist.id, plan)
    if not written:
        logger.info("artist_decided_during_matching", broadcast_artist_id=str(broadcast_artist.id))
    return written


@dataclass(frozen=True)
class ArtistMatchingRepos:
    """The repositories match_artists_for_playlist reads from and writes through.

    Every new collaborator this function needs means editing this dataclass
    and the one place that builds it (artist_matching_tasks.py), not the
    function signature — see AUD-040 and the precedent EnrichmentRepos set in
    library_enrichment_service.py.
    """

    broadcast_artist_repo: BroadcastArtistRepository
    track_identity_repo: BroadcastTrackIdentityRepository
    artist_repo: ArtistCatalogRepository
    # Unused since phase 1a moved the matcher's writes to log_name_links (S6); the locked
    # tests that build this dataclass still pass it.
    match_repo: MatchRepository
    rules_repo: MappingRuleRepository
    # The matcher's link writer (phase 1a S6). None: no links are written (statuses still
    # are); the tasks always pass it (tests/services/test_matching_repos_wiring.py).
    link_repo: LogNameLinkRepository | None = None
    # The not-an-artist list (phase 1c S6, C7): a listed name is never matched. None: nothing
    # is listed; the task and the dry run always pass it
    # (tests/services/test_placeholder_list_wiring.py).
    placeholders: LogNamePlaceholderRepository | None = None


def listed_names(
    names: Iterable[BroadcastArtist], placeholders: LogNamePlaceholderRepository | None
) -> set[str]:
    """The normalized names of *names* that are on the not-an-artist list."""
    if placeholders is None:
        return set()
    return placeholders.listed({name.normalized_name for name in names})


@dataclass(frozen=True)
class ArtistEngineInputs:
    """What the artist engines are built from (phase 0b, S6).

    match_artists_for_playlist builds its engines from one of these, and so does the gate's
    matching dry run (scripts/dry_run_matching.py), so a dry run decides what the task would.
    """

    repos: ArtistMatchingRepos
    mb_client: MusicBrainzClientProtocol
    thresholds: ArtistMatchThresholds


def build_local_artist_engine(inputs: ArtistEngineInputs) -> ArtistMatchingEngine:
    """Phase 1's engine: the mapping rules, then normalization against the local catalog.

    Reads the rules and every catalog artist once, when it is built.
    """
    thresholds = inputs.thresholds
    return ArtistMatchingEngine(
        [
            MappingRuleStrategy(inputs.repos.rules_repo.list_ordered()),
            NormalizationStrategy(
                inputs.repos.artist_repo.list_all(),
                thresholds.strong_match_threshold,
                thresholds.mb_score_gap,
                thresholds.mb_auto_link_score,
                min_presentation_score=thresholds.min_presentation_score,
            ),
        ]
    )


def build_mb_artist_engine(
    inputs: ArtistEngineInputs, search_map: dict[str, list[MbArtistResult]]
) -> ArtistMatchingEngine:
    """Phase 2's engine: MusicBrainz for a name that looks truncated, else DEFERRED_RETRY.

    Terminal: it decides every artist. ``search_map`` holds the coalesced searches
    (_build_truncated_search_map); a truncated name missing from it is searched live.
    """
    thresholds = inputs.thresholds
    return ArtistMatchingEngine(
        [
            TruncatedNameMbStrategy(
                inner=MusicBrainzApiStrategy(
                    inputs.mb_client,
                    thresholds.strong_match_threshold,
                    thresholds.mb_score_gap,
                    thresholds.mb_auto_link_score,
                    search_map=search_map,
                ),
                max_len=thresholds.broadcast_name_max_len,
            ),
            DeferredRetryStrategy(),
        ]
    )


def resolve_one_name(
    broadcast_artist: BroadcastArtist, *engines: ArtistMatchingEngine
) -> ArtistMatchResult:
    """The first decision *engines*, tried in order, make for one log name.

    The last engine must be terminal (build_mb_artist_engine's is). A MusicBrainz client
    error propagates.
    """
    for engine in engines:
        result = engine.resolve(broadcast_artist)
        if result is not None:
            return result
    # Invariant 2: DeferredRetryStrategy is the terminal tier and never
    # returns None. RuntimeError (not assert) so this stays load-bearing
    # under python -O, where asserts are stripped.
    raise RuntimeError(
        "Invariant 2 violated: Phase-2 engine returned None for "
        f"broadcast_artist {broadcast_artist.id}; DeferredRetryStrategy "
        "should make every artist terminal."
    )


def match_artists_for_playlist(
    playlist_id: UUID,
    repos: ArtistMatchingRepos,
    mb_client: MusicBrainzClientProtocol,
    thresholds: ArtistMatchThresholds = _DEFAULT_ARTIST_MATCH_THRESHOLDS,
    progress: MatchingProgress = NO_PROGRESS,
) -> None:
    """Resolve PENDING artists. Tier order:

    Phase 1 (every artist): MappingRuleStrategy → NormalizationStrategy.
    Phase 2 (Phase-1-unresolved): TruncatedNameMbStrategy → DeferredRetryStrategy.

    MB is consulted only for names that look truncated by the broadcast
    system's fixed-width field. Everything else parks in
    NEEDS_REVIEW/DEFERRED_RETRY for retry on the next ingestion (see
    artist_matching_task's reset paths).

    Persistence lives here, not in strategies. AUTO_REJECTED cascade
    preserved from legacy. ``progress`` ticks once per pending artist, after its result is
    persisted (spec 2026-10-17).

    A name on the not-an-artist list (phase 1c S6, E3) runs through no engine, so no
    MusicBrainz search is made for it: it gets ``listed_name_result``, and no cascade.
    """
    pending = repos.broadcast_artist_repo.get_pending_for_playlist(playlist_id)
    inputs = ArtistEngineInputs(repos=repos, mb_client=mb_client, thresholds=thresholds)
    local_engine = build_local_artist_engine(inputs)

    live_start = mb_client.live_fetches
    hits_start = mb_client.cache_hits
    distinct_search_keys = 0  # populated below; retained for the finally summary

    try:
        listed = listed_names(pending, repos.placeholders)
        to_match = [name for name in pending if name.normalized_name not in listed]
        resolved, unresolved = _run_local_phase(to_match, local_engine)
        search_map, distinct_search_keys = _build_truncated_search_map(
            unresolved,
            mb_client,
            thresholds.broadcast_name_max_len,
        )
        resolved.update(
            _run_mb_or_deferred_phase(unresolved, build_mb_artist_engine(inputs, search_map))
        )
        resolved.update(
            {name.id: listed_name_result() for name in pending if name.normalized_name in listed}
        )

        links = LogNameLinkService(
            repos.link_repo or NO_LINK_STORE, repos.artist_repo, repos.broadcast_artist_repo
        )
        deferred_artist_ids: list[UUID] = []
        for broadcast_artist in ticking(pending, progress):
            result = hold_back_refused_link(broadcast_artist, resolved[broadcast_artist.id], links)
            # Planned once: the status it writes comes from the name's links, so a suggested
            # name the matcher defers stays LINK_SUGGESTED and starts no cascade (S3's order).
            plan = links.plan_answer(broadcast_artist.id, matcher_answer(result))
            # Only artists this run actually wrote: one the user decided meanwhile keeps
            # its songs as they are (AUD-R018).
            if (
                _persist_artist_result(broadcast_artist, plan, links)
                and plan.state.reason_code == ReasonCode.DEFERRED_RETRY
            ):
                deferred_artist_ids.append(broadcast_artist.id)

        _cascade_auto_rejected(playlist_id, repos.broadcast_artist_repo, repos.track_identity_repo)
        _cascade_deferred(deferred_artist_ids, repos.track_identity_repo)
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


def _cascade_deferred(
    deferred_artist_ids: list[UUID],
    track_identity_repo: BroadcastTrackIdentityRepository,
) -> None:
    """Cascade: DEFERRED_RETRY artists -> defer their child identities.

    Mirrors _cascade_auto_rejected with retryable status (Invariant 6:
    cascade isolation). Caller passes the artist IDs this run wrote as
    DEFERRED_RETRY (the planned status, phase 1c S3), so a suggested name
    is never deferred. Avoids re-fetching reason_codes from the repo.

    Asymmetry note: _cascade_auto_rejected scans the playlist (legacy
    signature, retained to preserve its existing test contract). Aligning
    both signatures is left as a follow-up — see plan §"Out of Scope".
    """
    for artist_id in deferred_artist_ids:
        track_identity_repo.bulk_defer_by_artist(artist_id)

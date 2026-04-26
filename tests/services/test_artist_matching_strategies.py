"""Tests for the three ArtistMatchingStrategy implementations.

Strategies produce ArtistMatchResult values; they do not persist. The service
function (PR 4 Task 5) will wire them together and own persistence. These
tests exercise each strategy in isolation using in-memory fakes.
"""
from __future__ import annotations

from uuid import uuid4

from backend.domain.broadcast import BroadcastArtist
from backend.domain.catalog import Artist
from backend.domain.enums import MatchStatus, MatchTier, ReasonCode, TargetType
from backend.domain.matching import MappingRule
from backend.services.artist_matching_service import (
    MappingRuleStrategy,
    MusicBrainzApiStrategy,
    NormalizationStrategy,
)
from backend.services.matching_constants import MB_SCORE_GAP
from backend.services.normalization import normalize_artist
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.mb_client import FakeMbClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _broadcast_artist(
    original: str = "Metallica",
    normalized: str | None = None,
) -> BroadcastArtist:
    return BroadcastArtist(
        id=uuid4(),
        original_name=original,
        normalized_name=normalized if normalized is not None else original.lower(),
    )


def _canonical(
    name: str,
    artist_id: str | None = None,
    mbid: str | None = "__auto__",
) -> Artist:
    # Default mbid is a synthesized MBID-like string so canonicals are
    # downstream-usable by the MBID-graph identity tier. Pass `mbid=None`
    # explicitly to model a local-only row.
    resolved_mbid = f"mbid-{name}" if mbid == "__auto__" else mbid
    return Artist(
        id=artist_id or str(uuid4()),
        name=name,
        sort_name=name,
        mbid=resolved_mbid,
    )


def _rule(pattern: str, target_id: str, target_type: TargetType = TargetType.ARTIST) -> MappingRule:
    return MappingRule(
        id=uuid4(),
        source_pattern=pattern,
        target_type=target_type,
        target_id=target_id,
    )


# ---------------------------------------------------------------------------
# MappingRuleStrategy
# ---------------------------------------------------------------------------


def test_mapping_rule_hit() -> None:
    target_id = str(uuid4())
    strategy = MappingRuleStrategy(rules=[_rule("metallica", target_id)])
    result = strategy.apply(_broadcast_artist("Metallica", "metallica"))

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.MANUAL
    assert result.confidence_score == 100.0
    assert result.target_id == target_id
    assert result.reason_code is None


def test_mapping_rule_miss_returns_none() -> None:
    strategy = MappingRuleStrategy(rules=[_rule("metallica", str(uuid4()))])
    assert strategy.apply(_broadcast_artist("Foo", "foo")) is None


def test_mapping_rule_skips_non_artist_target_type() -> None:
    strategy = MappingRuleStrategy(
        rules=[_rule("metallica", str(uuid4()), target_type=TargetType.LIBRARY_FILE)]
    )
    assert strategy.apply(_broadcast_artist("Metallica", "metallica")) is None


# ---------------------------------------------------------------------------
# NormalizationStrategy
# ---------------------------------------------------------------------------


def test_normalization_exact_hit_auto_matches() -> None:
    canonical = _canonical("Metallica", mbid="mbid-metallica-xxx")
    strategy = NormalizationStrategy(all_canonical=[canonical])
    result = strategy.apply(_broadcast_artist("Metallica", "metallica"))

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.NORMALIZATION
    assert result.confidence_score == 100.0
    # target_id is the MBID (not the local UUID) so the MBID-graph identity tier works.
    assert result.target_id == "mbid-metallica-xxx"
    assert result.reason_code is None


def test_normalization_high_score_with_gap_auto_matches() -> None:
    # Pick a fuzzy near-miss of "metallica"; should still score high AND
    # the runner-up (totally unrelated) should be well below.
    best = _canonical("metallic")
    other = _canonical("radiohead")
    # Lower strong_match_threshold so ~94 score clears the AUTO_MATCHED bar via the
    # "strong_match_threshold + gap" branch rather than the MB_AUTO_LINK_SCORE bypass.
    strategy = NormalizationStrategy(
        all_canonical=[best, other],
        strong_match_threshold=80,
        mb_score_gap=10,
    )
    result = strategy.apply(_broadcast_artist("Metallica", "metallica"))

    assert result is not None
    # High score + large gap → AUTO_MATCHED
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.NORMALIZATION
    assert result.target_id == best.mbid
    assert result.confidence_score >= 90


def test_normalization_gap_insufficient_needs_review_ambiguous_gap() -> None:
    # Neither canonical normalizes to the target, but both score high
    # and close. broadcast normalized_name = "metalica" (typo)
    c1 = _canonical("metallica")
    c2 = _canonical("metallicca")
    strategy = NormalizationStrategy(
        all_canonical=[c1, c2],
        strong_match_threshold=80,
        mb_score_gap=50,  # force gap insufficiency
    )
    result = strategy.apply(_broadcast_artist("Metalica", "metalica"))

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    assert result.tier == MatchTier.NORMALIZATION
    assert result.reason_code == ReasonCode.AMBIGUOUS_GAP
    assert result.reason_detail is not None


def test_normalization_low_score_needs_review_low_confidence() -> None:
    # Only loosely similar canonicals → top score in the 50-64 band with
    # small gap (no MID_BAND auto-match), forces LOW_CONFIDENCE.
    c1 = _canonical("metal")
    c2 = _canonical("metal band")
    strategy = NormalizationStrategy(all_canonical=[c1, c2])
    result = strategy.apply(_broadcast_artist("Metalheads", "metalheads"))

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    assert result.tier == MatchTier.NORMALIZATION
    assert result.reason_code == ReasonCode.LOW_CONFIDENCE


def test_normalization_empty_canonical_returns_none() -> None:
    strategy = NormalizationStrategy(all_canonical=[])
    assert strategy.apply(_broadcast_artist()) is None


# ---------------------------------------------------------------------------
# Review fixes (mirrors identity side: has_competitor, noise floor fall-through,
# separate strong_match_threshold vs MB_AUTO_LINK_SCORE).
# ---------------------------------------------------------------------------


def test_normalization_lone_candidate_below_mid_band_needs_review_not_auto() -> None:
    """Issue #1: a SINGLE canonical scoring in the MID_BAND (55-64) must
    NEEDS_REVIEW, not AUTO_MATCH, because gap=100 is synthetic (no competitor)."""
    # Pick a canonical that fuzzy-scores in MID_BAND (55-64) against the broadcast.
    # "heavy metal" vs "metal" -> ~62.5 (inside MID_BAND), gap synthesized to 100.
    only = _canonical("heavy metal")
    strategy = NormalizationStrategy(all_canonical=[only])
    result = strategy.apply(_broadcast_artist("Metal", "metal"))

    assert result is not None
    # Top score is in MID_BAND but there's no real competitor: must NOT auto-match.
    assert result.status == MatchStatus.NEEDS_REVIEW
    assert result.reason_code == ReasonCode.LOW_CONFIDENCE


def test_mb_lone_candidate_mid_band_needs_review_not_auto() -> None:
    """Issue #1: a SINGLE MB candidate scoring in MID_BAND must NEEDS_REVIEW,
    not AUTO_MATCH, because second=0 makes gap=top_score (no real competitor)."""
    mb = FakeMbClient(responses={"Foo": [
        {"id": "mbid-only", "name": "Foo Bar", "score": 62},
    ]})
    repo = FakeArtistRepository()
    strategy = MusicBrainzApiStrategy(mb_client=mb, artist_repo=repo)
    result = strategy.apply(_broadcast_artist("Foo", "foo"))

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    # Must not upsert — only AUTO_MATCHED triggers the side effect.
    assert repo.list_all() == []


def test_normalization_below_noise_floor_falls_through_to_next_strategy() -> None:
    """Issue #2: canonicals exist but all fuzzy scores < MIN_PRESENTATION_SCORE
    (50) → apply() returns None so the engine can reach MusicBrainzApi."""
    # These names share almost nothing with "xyzzyqq" — scores will be well below 50.
    c1 = _canonical("metallica")
    c2 = _canonical("radiohead")
    strategy = NormalizationStrategy(all_canonical=[c1, c2])
    result = strategy.apply(_broadcast_artist("Xyzzyqq", "xyzzyqq"))

    assert result is None


def test_normalization_score_90_with_gap_auto_matches_via_strong_match_threshold() -> None:
    """Issue #3: a score of ~90-94 with sufficient gap must AUTO_MATCH via the
    strong_match_threshold(80)+gap(10) band. Previously broken because both ctor params
    were conflated to MB_AUTO_LINK_SCORE=95, so 90 < 95 forced NEEDS_REVIEW."""
    best = _canonical("metallic")  # fuzzy ~94 against "metallica"
    other = _canonical("radiohead")  # far away — gap is large
    # Rely on DEFAULT ctor (no kwargs) so we're testing the default separation
    # of strong_match_threshold=80 vs MB_AUTO_LINK_SCORE=95.
    strategy = NormalizationStrategy(all_canonical=[best, other])
    result = strategy.apply(_broadcast_artist("Metallica", "metallica"))

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.target_id == best.mbid
    # Score should be in the 80-94 band, i.e. strictly below MB_AUTO_LINK_SCORE(95)
    # — so the AUTO decision came through the strong_match_threshold+gap branch.
    assert result.confidence_score < 95


def test_mb_min_candidate_score_constant_lives_in_matching_constants() -> None:
    """Issue #4: MB_MIN_CANDIDATE_SCORE moved from class-level constant to
    matching_constants so all thresholds share one source of truth."""
    from backend.services import matching_constants

    assert hasattr(matching_constants, "MB_MIN_CANDIDATE_SCORE")
    assert matching_constants.MB_MIN_CANDIDATE_SCORE == 60


# ---------------------------------------------------------------------------
# MusicBrainzApiStrategy
# ---------------------------------------------------------------------------


def test_mb_strategy_high_score_auto_matches() -> None:
    mb_id = "mbid-1234"
    mb = FakeMbClient(responses={"Metallica": [
        {"id": mb_id, "name": "Metallica", "sort-name": "Metallica",
         "disambiguation": "US metal band", "score": 100},
        {"id": "mbid-other", "name": "Metallicka", "score": 50},
    ]})
    repo = FakeArtistRepository()
    strategy = MusicBrainzApiStrategy(mb_client=mb, artist_repo=repo)
    result = strategy.apply(_broadcast_artist("Metallica", "metallica"))

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.MUSICBRAINZ_API
    assert result.target_id == mb_id
    assert result.confidence_score == 100
    assert result.reason_code is None
    # Side-effect: MB result persisted to local catalog on AUTO_MATCHED via
    # upsert_musicbrainz_artist (NOT the generic upsert), so mbid/origin/
    # normalized_name are populated — required by later identity-tier lookups
    # keyed on artist mbid or normalized_name.
    stored = repo.get_by_id(mb_id)
    assert stored is not None
    assert stored.name == "Metallica"
    assert stored.disambiguation == "US metal band"
    assert stored.mbid == mb_id
    assert stored.normalized_name == normalize_artist("Metallica")


def test_mb_strategy_no_results_returns_none() -> None:
    mb = FakeMbClient(responses={})
    repo = FakeArtistRepository()
    strategy = MusicBrainzApiStrategy(mb_client=mb, artist_repo=repo)
    assert strategy.apply(_broadcast_artist("Unknown", "unknown")) is None


def test_mb_strategy_all_below_60_returns_none() -> None:
    mb = FakeMbClient(responses={"Fuzzy": [
        {"id": "a", "name": "A", "score": 55},
        {"id": "b", "name": "B", "score": 30},
    ]})
    repo = FakeArtistRepository()
    strategy = MusicBrainzApiStrategy(mb_client=mb, artist_repo=repo)
    assert strategy.apply(_broadcast_artist("Fuzzy", "fuzzy")) is None
    # No upsert should have happened.
    assert repo.list_all() == []


def test_mb_strategy_mid_score_needs_review_without_upsert() -> None:
    # Top candidate in 60-94 band with small gap (not in MID_BAND) → NEEDS_REVIEW
    # with LOW_CONFIDENCE, and the strategy must NOT upsert.
    mb = FakeMbClient(responses={"Metallica": [
        {"id": "mbid-top", "name": "Metallica Alt", "score": 70},
        {"id": "mbid-two", "name": "Metallic", "score": 68},
    ]})
    repo = FakeArtistRepository()
    strategy = MusicBrainzApiStrategy(mb_client=mb, artist_repo=repo)
    result = strategy.apply(_broadcast_artist("Metallica", "metallica"))

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    assert result.tier == MatchTier.MUSICBRAINZ_API
    assert result.reason_code == ReasonCode.LOW_CONFIDENCE
    assert result.target_id == "mbid-top"
    # NEEDS_REVIEW path must not touch local catalog.
    assert repo.list_all() == []


# ---------------------------------------------------------------------------
# Review fix #A: Match.target_id must be an MBID, not a local UUID.
# NormalizationStrategy must filter out canonicals with mbid=None (they cannot
# be used downstream by ResolvedArtistMbidStrategy which feeds
# library_file_repo.get_by_artist_mbid()).
# ---------------------------------------------------------------------------


def test_normalization_exact_hit_without_mbid_falls_through() -> None:
    """Exact-name canonical with mbid=None must NOT auto-match — it has no
    MBID so the identity-tier MBID-graph lookup would break silently."""
    canonical = _canonical("Prince", artist_id="local-uuid-1", mbid=None)
    strategy = NormalizationStrategy(all_canonical=[canonical])
    result = strategy.apply(_broadcast_artist("Prince", "prince"))

    assert result is None


def test_normalization_exact_hit_with_mbid_returns_mbid_as_target_id() -> None:
    """Exact-name canonical WITH mbid returns target_id=c.mbid (not c.id)."""
    canonical = _canonical("Prince", artist_id="local-uuid-1", mbid="mbid-prince-xxx")
    strategy = NormalizationStrategy(all_canonical=[canonical])
    result = strategy.apply(_broadcast_artist("Prince", "prince"))

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.NORMALIZATION
    assert result.target_id == "mbid-prince-xxx"
    assert result.target_id != canonical.id


def test_normalization_fuzzy_filters_out_canonicals_without_mbid() -> None:
    """Fuzzy pass must skip canonicals with mbid=None even if they would
    otherwise out-score the mbid-bearing ones."""
    no_mbid_winner = _canonical("metallica", mbid=None)  # exact would score highest
    mbid_runner_up = _canonical("metallic", mbid="mbid-metallic-ok")
    strategy = NormalizationStrategy(
        all_canonical=[no_mbid_winner, mbid_runner_up],
        strong_match_threshold=80,
        mb_score_gap=10,
    )
    result = strategy.apply(_broadcast_artist("Metallica", "metallica"))

    assert result is not None
    # Exact pass skips mbid=None; with only one mbid-bearing candidate left,
    # fuzzy AUTO_MATCHes it (no competitor check — gap synthesized to 100).
    assert result.target_id == "mbid-metallic-ok"
    # Sanity: the local UUID of the no-mbid canonical must NEVER appear.
    assert result.target_id != no_mbid_winner.id


def test_normalization_all_candidates_lack_mbid_returns_none() -> None:
    """If every canonical lacks an mbid, both exact and fuzzy passes fall
    through so the engine can reach MusicBrainzApiStrategy."""
    c1 = _canonical("Metallica", mbid=None)
    c2 = _canonical("Metallic", mbid=None)
    strategy = NormalizationStrategy(all_canonical=[c1, c2])
    result = strategy.apply(_broadcast_artist("Metallica", "metallica"))

    assert result is None


# ---------------------------------------------------------------------------
# Review fix #B: MusicBrainzApiStrategy must use upsert_musicbrainz_artist so
# mbid/origin/normalized_name are populated (the generic upsert leaves them
# NULL / LOCAL).
# ---------------------------------------------------------------------------


def test_mb_strategy_auto_matched_upserts_via_upsert_musicbrainz_artist() -> None:
    """AUTO_MATCHED must call upsert_musicbrainz_artist with mbid, name,
    sort_name, normalized_name (computed via normalize_artist), and
    disambiguation — NOT the generic .upsert()."""
    calls_generic: list[Artist] = []
    calls_mb: list[dict[str, object]] = []

    repo = FakeArtistRepository()
    original_upsert = repo.upsert
    original_upsert_mb = repo.upsert_musicbrainz_artist

    def spy_upsert(artist: Artist) -> Artist:
        calls_generic.append(artist)
        return original_upsert(artist)

    def spy_upsert_mb(
        mbid: str,
        name: str,
        sort_name: str,
        normalized_name: str,
        disambiguation: str | None = None,
    ) -> str:
        calls_mb.append({
            "mbid": mbid,
            "name": name,
            "sort_name": sort_name,
            "normalized_name": normalized_name,
            "disambiguation": disambiguation,
        })
        return original_upsert_mb(mbid, name, sort_name, normalized_name, disambiguation)

    repo.upsert = spy_upsert  # type: ignore[method-assign]
    repo.upsert_musicbrainz_artist = spy_upsert_mb  # type: ignore[method-assign]

    mb = FakeMbClient(responses={"Metallica": [
        {"id": "mbid-m", "name": "Metallica", "sort-name": "Metallica",
         "disambiguation": "US metal band", "score": 100},
    ]})
    strategy = MusicBrainzApiStrategy(mb_client=mb, artist_repo=repo)
    result = strategy.apply(_broadcast_artist("Metallica", "metallica"))

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    # target_id is still the MBID from the MB payload, NOT the upsert return.
    assert result.target_id == "mbid-m"

    assert calls_generic == []
    assert len(calls_mb) == 1
    assert calls_mb[0] == {
        "mbid": "mbid-m",
        "name": "Metallica",
        "sort_name": "Metallica",
        "normalized_name": normalize_artist("Metallica"),
        "disambiguation": "US metal band",
    }


# ---------------------------------------------------------------------------
# Determinism + operator configurability (review round 4)
# ---------------------------------------------------------------------------


def test_normalization_fuzzy_ties_break_deterministically_by_mbid() -> None:
    """Two distinct canonicals that fuzzy-score identically vs the target:
    the winner must be the one with the lower mbid string (stable tie-break),
    regardless of input order. Uses names that don't normalize to the target
    (so the exact-hit short-circuit doesn't fire).
    """
    # "Metalica" and "Metallicka" both fuzzy-score against "metallica"; when
    # tokens are single-word, token_sort_ratio falls back to levenshtein
    # ratio — both typo variants produce similar but distinct scores. Use
    # two synonyms that token-sort to identical character sets.
    # "A B" and "B A" produce the same token_sort_ratio.
    a = _canonical("Alpha Beta", mbid="mbid-zzz")
    b = _canonical("Beta Alpha", mbid="mbid-aaa")
    broadcast = _broadcast_artist(normalized="alpha beta gamma")

    result_ab = NormalizationStrategy(
        [a, b], strong_match_threshold=80, mb_score_gap=MB_SCORE_GAP
    ).apply(broadcast)
    result_ba = NormalizationStrategy(
        [b, a], strong_match_threshold=80, mb_score_gap=MB_SCORE_GAP
    ).apply(broadcast)

    assert result_ab is not None and result_ba is not None
    # Both orderings pick the same winner — the lower mbid when scores tie.
    assert result_ab.target_id == result_ba.target_id == "mbid-aaa"


def test_mb_strategy_ties_break_deterministically_by_id() -> None:
    fake = FakeMbClient(
        responses={
            "Metallica": [
                {"id": "mbid-zzz", "name": "Metallica", "score": 95},
                {"id": "mbid-aaa", "name": "Metallica", "score": 95},
            ]
        }
    )
    repo = FakeArtistRepository()
    strat = MusicBrainzApiStrategy(fake, repo, strong_match_threshold=80, mb_score_gap=MB_SCORE_GAP)
    result = strat.apply(_broadcast_artist(original="Metallica", normalized="metallica"))
    assert result is not None
    # Lower id wins the tie-break.
    assert result.target_id == "mbid-aaa"


def test_mb_auto_link_score_operator_override_raises_threshold() -> None:
    """Operator can make AUTO harder to achieve by raising mb_auto_link_score.
    Use two candidates with a small gap so the unconditional-AUTO gate
    (mb_auto_link_score) is the decisive clause — the strong_match_threshold+gap
    clause is blocked by the small gap, so the override is observable.
    """
    responses = {
        "Metallica": [
            {"id": "mbid-a", "name": "Metallica", "score": 95},
            {"id": "mbid-b", "name": "Metallika", "score": 90},  # gap=5 < MB_SCORE_GAP(10)
        ]
    }

    # Default: score 95 clears the unconditional MB_AUTO_LINK_SCORE gate.
    default_result = MusicBrainzApiStrategy(
        FakeMbClient(responses=responses),
        FakeArtistRepository(),
        strong_match_threshold=80, mb_score_gap=MB_SCORE_GAP,
    ).apply(_broadcast_artist(original="Metallica", normalized="metallica"))
    assert default_result is not None
    assert default_result.status == MatchStatus.AUTO_MATCHED

    # Override: raise the gate to 98. Score 95 no longer clears it; the
    # strong_match_threshold+gap clause is blocked by gap=5 < score_gap(10);
    # mid-band clause doesn't fire (score 95 is above the band). Falls to
    # NEEDS_REVIEW with AMBIGUOUS_GAP reason.
    overridden = MusicBrainzApiStrategy(
        FakeMbClient(responses=responses),
        FakeArtistRepository(),
        strong_match_threshold=80, mb_score_gap=MB_SCORE_GAP,
        mb_auto_link_score=98,
    ).apply(_broadcast_artist(original="Metallica", normalized="metallica"))
    assert overridden is not None
    assert overridden.status == MatchStatus.NEEDS_REVIEW
    assert overridden.reason_code == ReasonCode.AMBIGUOUS_GAP


# ---------------------------------------------------------------------------
# Lone-candidate high-band guard (correctness review follow-up)
# ---------------------------------------------------------------------------


def test_normalization_lone_candidate_high_band_needs_review_not_auto() -> None:
    """A lone canonical with score >= strong_match_threshold must NOT auto-match.

    Without the `has_competitor` guard on the strong_match_threshold+gap clause,
    a single canonical with synthesized gap=100 would pass the auto-match
    test silently. A single match at any score below the unconditional-AUTO
    gate (95) is genuinely unverifiable, so it belongs in NEEDS_REVIEW.
    """
    # "metalica" vs "metallica" scores ~94 — above strong_match_threshold (80),
    # below MB_AUTO_LINK_SCORE (95).
    bc = _broadcast_artist(normalized="metalica")
    only = _canonical("Metallica", mbid="mbid-metallica-xxx")

    result = NormalizationStrategy(
        [only], strong_match_threshold=80, mb_score_gap=MB_SCORE_GAP,
    ).apply(bc)

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    # Reason must be LOW_CONFIDENCE, not AMBIGUOUS_GAP — there's no peer
    # to be ambiguous with, so the "within N points" message would be
    # nonsensical if rendered.
    assert result.reason_code == ReasonCode.LOW_CONFIDENCE


def test_mb_lone_candidate_high_band_needs_review_not_auto() -> None:
    """Same guard for the MusicBrainz strategy: a lone MB result at score 90
    must not auto-match via the synthesized gap = top_score - 0 = 90 path.
    """
    fake = FakeMbClient(
        responses={
            "Metallica": [{"id": "mbid-m", "name": "Metallica", "score": 90}],
        }
    )
    result = MusicBrainzApiStrategy(
        fake, FakeArtistRepository(),
        strong_match_threshold=80, mb_score_gap=MB_SCORE_GAP,
    ).apply(_broadcast_artist(original="Metallica", normalized="metallica"))

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    assert result.reason_code == ReasonCode.LOW_CONFIDENCE


def test_mb_lone_candidate_above_auto_link_still_auto_matches() -> None:
    """The unconditional-AUTO gate (MB_AUTO_LINK_SCORE, 95) intentionally
    does NOT require has_competitor. At >= 95 a token match is effectively
    a literal identity — a lone result at that confidence is still safe to
    auto-link. This test pins that behavior so the guard can't be
    over-applied in a later refactor.
    """
    fake = FakeMbClient(
        responses={
            "Metallica": [{"id": "mbid-m", "name": "Metallica", "score": 98}],
        }
    )
    result = MusicBrainzApiStrategy(
        fake, FakeArtistRepository(),
        strong_match_threshold=80, mb_score_gap=MB_SCORE_GAP,
    ).apply(_broadcast_artist(original="Metallica", normalized="metallica"))

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED


# ---------------------------------------------------------------------------
# search_map (Step 1 coalescing): strategy-level behavior
# The map is populated by a pre-pass in match_artists_for_playlist. The
# strategy itself only reads it. Contract:
#   key absent:  strategy falls back to live search_artist.
#   key present, []:  treat as "no candidates", return None, no live call.
#   key present, [...]: use those results, no live call.
# Key = broadcast_artist.original_name.lower() — exactly matches the cache
# key suffix `artist-search:{name.lower()}` so coalescing is identical to
# the cache's bucketing.
# ---------------------------------------------------------------------------


def test_mb_strategy_uses_search_map_when_key_present() -> None:
    # Map hit must satisfy the apply() call with zero live search_artist calls.
    mb = FakeMbClient(responses={})
    repo = FakeArtistRepository()
    search_map: dict[str, list[dict[str, object]]] = {
        "metallica": [
            {"id": "mbid-m", "name": "Metallica", "sort-name": "Metallica",
             "disambiguation": "US metal band", "score": 100},
        ],
    }
    strategy = MusicBrainzApiStrategy(mb_client=mb, artist_repo=repo, search_map=search_map)
    result = strategy.apply(_broadcast_artist("Metallica", "metallica"))

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.target_id == "mbid-m"
    assert mb.calls == []  # no live fetch


def test_mb_strategy_empty_list_in_map_returns_none_no_live_call() -> None:
    # Empty list is the "no candidates from MB" sentinel — must NOT retry live.
    mb = FakeMbClient(responses={"Obscure Band": [
        {"id": "mbid-x", "name": "Obscure Band", "score": 100},
    ]})
    repo = FakeArtistRepository()
    search_map: dict[str, list[dict[str, object]]] = {"obscure band": []}
    strategy = MusicBrainzApiStrategy(mb_client=mb, artist_repo=repo, search_map=search_map)
    result = strategy.apply(_broadcast_artist("Obscure Band", "obscure band"))

    assert result is None
    assert mb.calls == []  # sentinel short-circuits live fetch


def test_mb_strategy_missing_key_falls_back_to_live() -> None:
    # Key absent from map — strategy must call search_artist directly
    # (happens when the pre-pass failed for that key, or the row was added
    # after the pre-pass).
    mb = FakeMbClient(responses={"Metallica": [
        {"id": "mbid-m", "name": "Metallica", "sort-name": "Metallica", "score": 100},
    ]})
    repo = FakeArtistRepository()
    search_map: dict[str, list[dict[str, object]]] = {"other band": []}
    strategy = MusicBrainzApiStrategy(mb_client=mb, artist_repo=repo, search_map=search_map)
    result = strategy.apply(_broadcast_artist("Metallica", "metallica"))

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert mb.calls == ["Metallica"]  # exactly one live fallback call


def test_mb_strategy_search_map_key_is_lowercased() -> None:
    # Cache key convention is `artist-search:{name.lower()}`.
    # Strategy must look up the map with `.lower()` so bucket identity matches.
    mb = FakeMbClient(responses={})
    repo = FakeArtistRepository()
    search_map: dict[str, list[dict[str, object]]] = {
        "metallica": [
            {"id": "mbid-m", "name": "Metallica", "sort-name": "Metallica", "score": 100},
        ],
    }
    strategy = MusicBrainzApiStrategy(mb_client=mb, artist_repo=repo, search_map=search_map)
    # Note the mixed case — must still hit the "metallica" bucket.
    result = strategy.apply(_broadcast_artist("MeTaLLiCa", "metallica"))

    assert result is not None
    assert result.target_id == "mbid-m"
    assert mb.calls == []

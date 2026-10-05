"""AUD-014 gate 1 characterisation tests for title-scoring policy.

Locks the observable behaviour of broadcast/library title scoring. Written
before it moved out of ``backend/services/matching_utils.py`` and
``backend/services/identity_matching_service.py`` into a single
``backend/services/title_scoring.py`` module (AUD-014, PR #95). Only the
imports below changed in that refactor commit — this file's snapshot
(``__snapshots__/test_title_scoring_characterisation.ambr``) stayed
byte-identical across it.

No Postgres, no network: everything here is a pure function or an in-memory
``LibraryFile`` dataclass built with deterministic UUIDs so the sort-by-id
tie-break in ``_score_candidates`` is reproducible across runs.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from syrupy.assertion import SnapshotAssertion

from backend.domain.enums import MatchTier
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.identity_matching_service import _score_candidates
from backend.services.title_scoring import (
    _candidate_scores,
    broadcast_title_core_variants,
    broadcast_title_variants,
    library_title_variants,
    normalize_title_for_scoring,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _lib_file(
    seed: int,
    track_title: str | None = None,
    normalized_title: str | None = None,
) -> LibraryFile:
    """A minimal LibraryFile with a deterministic UUID.

    seed drives id=UUID(int=seed) so the id-ascending tie-break in
    _score_candidates is reproducible across every run (a random uuid4()
    would make the tie-break snapshot non-reproducible).
    """
    return LibraryFile(
        id=UUID(int=seed),
        file_path=f"/music/{seed}.flac",
        format="flac",
        audio=AudioMetadata(track_title=track_title, normalized_title=normalized_title),
    )


def _serialize_result(result: object) -> dict[str, object]:
    """Flatten IdentityMatchResult to plain values for a stable snapshot repr."""
    return {
        "status": result.status.value,  # type: ignore[attr-defined]
        "tier": result.tier.value,  # type: ignore[attr-defined]
        "confidence_score": result.confidence_score,  # type: ignore[attr-defined]
        "library_file_id": (
            str(result.library_file_id)  # type: ignore[attr-defined]
            if result.library_file_id  # type: ignore[attr-defined]
            else None
        ),
        "work_id": result.work_id,  # type: ignore[attr-defined]
        "reason_code": (
            result.reason_code.value if result.reason_code else None  # type: ignore[attr-defined]
        ),
        "reason_detail": result.reason_detail,  # type: ignore[attr-defined]
    }


# ---------------------------------------------------------------------------
# Part 1: variant functions
#
# (id, broadcast_title, library track_title, library normalized_title)
# ---------------------------------------------------------------------------

VARIANT_CASES: list[tuple[str, str, str | None, str | None]] = [
    ("plain_no_credit", "Enter Sandman", "Enter Sandman", None),
    ("f_slash_credit", "Smooth f/Rob Thomas", "Smooth", None),
    (
        "f_slash_credit_lib_has_feat_clause",
        "Smooth f/Rob Thomas",
        "Smooth (Feat. Rob Thomas)",
        None,
    ),
    ("f_slash_after_ellipsis", "Livin' My Life Like..f/C.Marks", "Livin My Life Like", None),
    ("f_slash_in_parens", "Smooth (f/Rob Thomas)", "Smooth", None),
    ("slash_inside_word_not_a_credit", "Rif/Raf", "Rif Raf", None),
    ("w_slash_offers_both_forms", "The First Noel w/Faith Hill", "The First Noel", None),
    (
        "w_slash_is_part_of_the_title",
        "Killing Me Softly W/His Song",
        "Killing Me Softly With His Song",
        None,
    ),
    (
        "w_slash_o_means_without",
        "I Don't Want To Live W/o You",
        "I Don't Want To Live Without You",
        None,
    ),
    (
        "w_slash_out_means_without",
        "I Don't Wanna Live W/out Your",
        "I Dont Wanna Live Without Your",
        None,
    ),
    ("title_that_is_only_a_credit", "f/Nobody", "Nobody", None),
    ("bracketed_alt_title_stand_by_me", "Train In Vain (Stand By Me)", "Train in Vain", None),
    ("bracketed_alt_title_leading", "(Keep Feeling) Fascination", "Fascination", None),
    ("part_number_i_not_stripped", "Disco Duck (Part I)", "Disco Duck (Part I)", None),
    ("part_number_ii_not_stripped", "Disco Duck (Part II)", "Disco Duck (Part II)", None),
    ("credit_and_bracket_combine", "Smooth (Radio Edit) f/Rob Thomas", "Smooth", None),
    (
        "brass_in_pocket_stored_normalized_title",
        "Brass In Pocket (I'm Special)",
        "Brass in Pocket (I'm Special)",
        "brass in pocket im special",
    ),
    ("halo_broadcast_plain_lib_has_live_tag", "Halo", "Halo (Live)", None),
    ("halo_broadcast_live_lib_plain", "Halo (Live)", "Halo", None),
    ("missing_lp_version", "Missing", "Missing [LP Version]", None),
    ("missing_plain_both_sides", "Missing", "Missing", None),
    ("hero_metro_mix_radio_edit", "Hero", "Hero (Metro Mix Radio Edit)", None),
    ("la_song_bracketed_subtitle", "L.A. Song", "L.A. Song (Out of This Town)", None),
    ("my_girl_vs_hey_girl", "My Girl", "Hey Girl (I Like Your Style)", None),
    ("purple_rain_live_broadcast_plain_lib", "Purple Rain (Live)", "Purple Rain", None),
    ("purple_rain_live_both_sides", "Purple Rain (Live)", "Purple Rain (Live)", None),
    (
        "sweet_dreams_no_space_before_paren",
        "Sweet Dreams(Are Made Of This)",
        "Sweet Dreams (Are Made of This)",
        None,
    ),
    ("feat_clause_parenthesised", "Song (feat. Artist)", "Song", None),
    ("feat_clause_bare", "Song feat. Artist", "Song", None),
    ("radio_edit_suffix", "Song (Radio Edit)", "Song", None),
    ("remastered_suffix", "Song (Remastered)", "Song", None),
    ("brackets_instead_of_parens", "Song [Live]", "Song", None),
    ("cry_baby_cry_vs_baby_its_you_mono", "Cry Baby Cry", "Baby It's You [Mono]", None),
    ("enter_sandman_vs_one_letter_typo", "Enter Sandman", "Enter Sandmen", None),
    ("enter_sandman_vs_partial_title", "Enter Sandman", "Sandman Returns", None),
    ("enter_sandman_vs_unrelated", "Enter Sandman", "Complete Unrelated Junk Words Here", None),
    ("star_spangled_banner_leading_the", "The Star Spangled Banner", "Star Spangled Banner", None),
    ("case_insensitive_title", "Jump", "jump", None),
    ("missing_track_title_falls_back_to_stored_form", "Missing", None, "missing"),
    ("missing_track_title_and_normalized_title", "Enter Sandman", None, None),
    ("hello_live_both_sides", "Hello (Live)", "Hello (Live)", None),
    ("hello_live_broadcast_plain_lib", "Hello (Live)", "Hello", None),
    ("disco_duck_mismatched_parts", "Disco Duck (Part I)", "Disco Duck (Part II)", None),
    (
        "w_slash_credit_after_apostrophe_title",
        "Killing Me Softly W/His Song",
        "Killing Me Softly",
        None,
    ),
]


@pytest.mark.parametrize(
    "broadcast_title,track_title,normalized_title",
    [c[1:] for c in VARIANT_CASES],
    ids=[c[0] for c in VARIANT_CASES],
)
def test_variant_functions(
    broadcast_title: str,
    track_title: str | None,
    normalized_title: str | None,
    snapshot: SnapshotAssertion,
) -> None:
    result = {
        "broadcast_title_variants": broadcast_title_variants(broadcast_title),
        "broadcast_title_core_variants": broadcast_title_core_variants(broadcast_title),
        "library_title_variants": library_title_variants(track_title, normalized_title),
        "normalize_title_for_scoring_broadcast": normalize_title_for_scoring(broadcast_title),
        "normalize_title_for_scoring_track_title": (
            normalize_title_for_scoring(track_title) if track_title else None
        ),
    }
    assert result == snapshot


# ---------------------------------------------------------------------------
# Part 2: _candidate_scores
#
# (id, broadcast_title, library track_title, library normalized_title, threshold)
# ---------------------------------------------------------------------------

CANDIDATE_SCORE_CASES: list[tuple[str, str, str | None, str | None, int]] = [
    ("exact_match", "Enter Sandman", "Enter Sandman", None, 80),
    ("f_credit_stripped_exact", "Smooth f/Rob Thomas", "Smooth", None, 80),
    ("f_credit_lib_has_feat_clause", "Smooth f/Rob Thomas", "Smooth (Feat. Rob Thomas)", None, 80),
    ("w_credit_scores_better_stripped", "The First Noel w/Faith Hill", "The First Noel", None, 80),
    (
        "w_credit_is_part_of_title",
        "Killing Me Softly W/His Song",
        "Killing Me Softly With His Song",
        None,
        80,
    ),
    ("bracket_alt_title_broadcast_side", "Train In Vain (Stand By Me)", "Train in Vain", None, 80),
    (
        "bracket_alt_title_library_side",
        "Brass in Pocket",
        "Brass in Pocket (I'm Special)",
        "brass in pocket im special",
        80,
    ),
    ("stripped_below_threshold_ignored", "Cry Baby Cry", "Baby It's You [Mono]", None, 80),
    ("live_suffix_full_match", "Purple Rain (Live)", "Purple Rain (Live)", None, 80),
    ("live_suffix_vs_studio", "Purple Rain (Live)", "Purple Rain", None, 80),
    ("part_number_i", "Disco Duck (Part I)", "Disco Duck (Part I)", None, 80),
    ("part_number_mismatch", "Disco Duck (Part I)", "Disco Duck (Part II)", None, 80),
    ("typo_close_match", "Enter Sandman", "Enter Sandmen", None, 80),
    ("totally_unrelated", "Enter Sandman", "Complete Unrelated Junk Words Here", None, 80),
    ("hero_vs_metro_mix", "Hero", "Hero (Metro Mix Radio Edit)", None, 80),
    ("missing_vs_lp_version", "Missing", "Missing [LP Version]", None, 80),
    ("la_song_vs_bracketed", "L.A. Song", "L.A. Song (Out of This Town)", None, 80),
    ("my_girl_vs_hey_girl", "My Girl", "Hey Girl (I Like Your Style)", None, 80),
    ("missing_persons_mid_band", "Missing", "Missing Persons", None, 80),
    ("missing_in_action_mid_band", "Missing", "Missing In Action", None, 80),
    ("low_threshold_lets_stripped_score_through", "Missing", "Missing [LP Version]", None, 50),
    ("high_threshold_falls_back_to_full_score", "Hero", "Hero (Metro Mix Radio Edit)", None, 101),
]


@pytest.mark.parametrize(
    "broadcast_title,track_title,normalized_title,threshold",
    [c[1:] for c in CANDIDATE_SCORE_CASES],
    ids=[c[0] for c in CANDIDATE_SCORE_CASES],
)
def test_candidate_scores(
    broadcast_title: str,
    track_title: str | None,
    normalized_title: str | None,
    threshold: int,
    snapshot: SnapshotAssertion,
) -> None:
    full_bcs = [normalize_title_for_scoring(t) for t in broadcast_title_variants(broadcast_title)]
    core_bcs = [
        normalize_title_for_scoring(t) for t in broadcast_title_core_variants(broadcast_title)
    ]
    f = _lib_file(1, track_title=track_title, normalized_title=normalized_title)
    result = _candidate_scores(full_bcs, core_bcs, f, threshold)
    assert result == snapshot


# ---------------------------------------------------------------------------
# Part 3: _score_candidates — multi-candidate lists, including ties
# ---------------------------------------------------------------------------


def test_score_candidates_single_candidate_exact_auto_matches(
    snapshot: SnapshotAssertion,
) -> None:
    candidate = _lib_file(1, track_title="Enter Sandman")
    result = _score_candidates("Enter Sandman", [candidate], MatchTier.LOCAL_FILE_FUZZY, 80)
    assert _serialize_result(result) == snapshot


def test_score_candidates_lone_high_band_needs_review(snapshot: SnapshotAssertion) -> None:
    """Lone candidate at ~92 (above strong_match_threshold, below MB_AUTO_LINK_SCORE)
    must NOT auto-match — the synthesized gap=100 would otherwise trigger it."""
    candidate = _lib_file(1, track_title="Enter Sandmen")
    result = _score_candidates("Enter Sandman", [candidate], MatchTier.LOCAL_FILE_FUZZY, 80)
    assert _serialize_result(result) == snapshot


def test_score_candidates_lone_below_threshold_needs_review(
    snapshot: SnapshotAssertion,
) -> None:
    candidate = _lib_file(1, track_title="Sandman Returns")
    result = _score_candidates("Enter Sandman", [candidate], MatchTier.LOCAL_FILE_FUZZY, 80)
    assert _serialize_result(result) == snapshot


def test_score_candidates_ambiguous_gap_two_close_candidates(
    snapshot: SnapshotAssertion,
) -> None:
    a = _lib_file(3, track_title="Enter Sandmen")
    b = _lib_file(4, track_title="Enter Sandman Jr")
    result = _score_candidates("Enter Sandman", [a, b], MatchTier.LOCAL_FILE_FUZZY, 80)
    assert _serialize_result(result) == snapshot


def test_score_candidates_ambiguous_gap_three_candidates(snapshot: SnapshotAssertion) -> None:
    a = _lib_file(1, track_title="Enter Sandmen")
    b = _lib_file(2, track_title="Enter Sandman Jr")
    c = _lib_file(3, track_title="Enter Sand Man")
    result = _score_candidates("Enter Sandman", [a, b, c], MatchTier.LOCAL_FILE_FUZZY, 80)
    assert _serialize_result(result) == snapshot


def test_score_candidates_mid_band_gap_auto_matches(snapshot: SnapshotAssertion) -> None:
    top = _lib_file(2, track_title="Missing Persons")
    low = _lib_file(1, track_title="Totally Unrelated Words Here")
    result = _score_candidates("Missing", [top, low], MatchTier.LOCAL_FILE_FUZZY, 80)
    assert _serialize_result(result) == snapshot


def test_score_candidates_tie_break_by_lowest_id(snapshot: SnapshotAssertion) -> None:
    """Two candidates score identically (both exact matches); the lower
    UUID wins deterministically — without the id tie-break this would depend
    on (often non-deterministic) input order."""
    higher_id = _lib_file(5, track_title="Enter Sandman")
    lower_id = _lib_file(2, track_title="Enter Sandman")
    result = _score_candidates(
        "Enter Sandman", [higher_id, lower_id], MatchTier.LOCAL_FILE_FUZZY, 80
    )
    assert _serialize_result(result) == snapshot
    assert result.library_file_id == lower_id.id


def test_score_candidates_exact_bracket_wins_tie_over_stripped(
    snapshot: SnapshotAssertion,
) -> None:
    """ "Hello (Live)" picks the live file over "Hello" when both reach 100:
    the full-form score breaks the tie in favour of the file whose tag
    carries the same bracketed text as the broadcast title."""
    live = _lib_file(1, track_title="Hello (Live)")
    studio = _lib_file(2, track_title="Hello")
    result = _score_candidates("Hello (Live)", [live, studio], MatchTier.LOCAL_FILE_FUZZY, 80)
    assert _serialize_result(result) == snapshot
    assert result.library_file_id == live.id


def test_score_candidates_part_numbers_still_tell_songs_apart(
    snapshot: SnapshotAssertion,
) -> None:
    part1 = _lib_file(1, track_title="Disco Duck (Part I)")
    part2 = _lib_file(2, track_title="Disco Duck (Part II)")
    result = _score_candidates(
        "Disco Duck (Part I)", [part1, part2], MatchTier.LOCAL_FILE_FUZZY, 80
    )
    assert _serialize_result(result) == snapshot
    assert result.library_file_id == part1.id


def test_score_candidates_tier_is_passed_through_verbatim(snapshot: SnapshotAssertion) -> None:
    candidate = _lib_file(9, track_title="Totally Different Words")
    result = _score_candidates("Enter Sandman", [candidate], MatchTier.MUSICBRAINZ_ID_SEARCH, 80)
    assert _serialize_result(result) == snapshot


def test_score_candidates_requires_at_least_one_candidate() -> None:
    with pytest.raises(ValueError, match="requires at least one candidate"):
        _score_candidates("Enter Sandman", [], MatchTier.LOCAL_FILE_FUZZY, 80)

"""Unit tests for normalization service (FR12–FR25)."""

from backend.domain.enums import VersionType
from backend.services.normalization import (
    classify_version_descriptor,
    compute_normalized_signature,
    detect_embedded_remix,
    extract_dash_version,
    extract_version_tags,
    normalize_artist,
    normalize_title,
    split_artist_string,
)

# ---------------------------------------------------------------------------
# FR12–FR18: Core pipeline (updated assertions for article stripping FR19)
# ---------------------------------------------------------------------------


def test_normalize_artist_smart_quotes() -> None:
    """Smart quotes are normalized to ASCII."""
    assert normalize_artist("Artist\u2019s Name") == "artists name"
    assert normalize_artist("\u201cHello\u201d") == "hello"


def test_normalize_artist_strip_accents() -> None:
    """Accents and diacritics are stripped (FR14)."""
    assert normalize_artist("Café") == "cafe"
    assert normalize_artist("Zürich") == "zurich"
    assert normalize_artist("Björk") == "bjork"


def test_normalize_artist_lowercase() -> None:
    """Output is lowercased; leading article 'the' is stripped (FR19)."""
    # "THE BEATLES" → lower → "the beatles" → strip leading article → "beatles"
    assert normalize_artist("THE BEATLES") == "beatles"


def test_normalize_artist_remaster_and_year_removed() -> None:
    """Remaster and year brackets removed (FR15, FR16)."""
    assert "(Remastered 2023)" not in normalize_artist("Band (Remastered 2023)")
    assert normalize_artist("Band (2022)") == "band"
    assert normalize_artist("Artist [1994]") == "artist"


def test_normalize_artist_truncation_removed() -> None:
    """Truncation markers (...) and [...] removed (FR13)."""
    assert "..." not in normalize_artist("Artist (...)")
    assert normalize_artist("Name [\u2026]") == "name"


def test_normalize_artist_whitespace_collapsed() -> None:
    """Whitespace is collapsed to single spaces."""
    assert normalize_artist("  Multiple   Spaces  ") == "multiple spaces"


def test_normalize_title_same_pipeline() -> None:
    """Title uses same core pipeline as before."""
    assert normalize_title("Song (Remastered 2023)") == "song"
    assert normalize_title("Café Song") == "cafe song"


def test_compute_normalized_signature_deterministic() -> None:
    """Signature is deterministic 32-char hex (FR18)."""
    sig = compute_normalized_signature("artist", "title")
    assert len(sig) == 32
    assert all(c in "0123456789abcdef" for c in sig)
    assert compute_normalized_signature("artist", "title") == sig


def test_compute_normalized_signature_different_inputs_different_sig() -> None:
    """Different artist/title produce different signatures."""
    s1 = compute_normalized_signature("a", "b")
    s2 = compute_normalized_signature("a", "c")
    s3 = compute_normalized_signature("b", "b")
    assert s1 != s2 != s3


def test_normalize_empty_strings() -> None:
    """Empty or whitespace-only input returns empty string."""
    assert normalize_artist("") == ""
    assert normalize_artist("   ") == ""
    assert normalize_title("") == ""


# ---------------------------------------------------------------------------
# FR19: Article stripping in normalize_artist
# ---------------------------------------------------------------------------


def test_normalize_artist_strips_leading_the() -> None:
    """Leading 'the' (any case) is stripped from artist (FR19)."""
    assert normalize_artist("The Beatles") == "beatles"
    assert normalize_artist("THE ROLLING STONES") == "rolling stones"
    assert normalize_artist("the cure") == "cure"


def test_normalize_artist_strips_leading_a() -> None:
    """Leading article 'a' (as a whole word) is stripped (FR19)."""
    assert normalize_artist("A Tribe Called Quest") == "tribe called quest"


def test_normalize_artist_strips_leading_an() -> None:
    """Leading article 'an' (as a whole word) is stripped (FR19)."""
    assert normalize_artist("An Artist") == "artist"


def test_normalize_artist_does_not_strip_mid_string_article() -> None:
    """Articles in the middle of a name are NOT stripped — only leading."""
    # "Earth the Band": leading word is "earth", not an article → no strip
    assert normalize_artist("Earth the Band") == "earth the band"
    # "Sun and Moon": "and" is not an article, no stripping occurs
    assert normalize_artist("Sun and Moon") == "sun and moon"
    # "The Sun and The Moon": only the LEADING "the" is stripped;
    # mid-string "the" before "moon" stays → "sun and the moon".
    assert normalize_artist("The Sun and The Moon") == "sun and the moon"


def test_normalize_artist_hyphenated_article_not_stripped() -> None:
    """Hyphenated prefix like 'The-Clash' bypasses article strip (L4).

    _ARTICLE_RE requires \\s+ after the article; a hyphen is not whitespace,
    so 'the-clash' → no strip → hyphen→space → 'the clash'.  Documents this
    known edge-case so the threshold is not accidentally lowered.
    """
    assert normalize_artist("The-Clash") == "the clash"


def test_normalize_artist_no_article_unchanged() -> None:
    """Artist without a leading article is unchanged by article stripping."""
    assert normalize_artist("Pink Floyd") == "pink floyd"
    assert normalize_artist("Radiohead") == "radiohead"


# ---------------------------------------------------------------------------
# FR12 / FR19: Feat. suffix stripping in normalize_artist
# ---------------------------------------------------------------------------


def test_normalize_artist_strips_feat_paren() -> None:
    """'(feat. X)' suffix is stripped from artist string."""
    assert normalize_artist("Artist (feat. Yoko Ono)") == "artist"


def test_normalize_artist_strips_feat_inline() -> None:
    """Inline 'feat. X' at end is stripped."""
    assert normalize_artist("Artist feat. Yoko Ono") == "artist"


def test_normalize_artist_strips_ft_paren() -> None:
    """'(ft. X)' abbreviation is stripped."""
    assert normalize_artist("Artist (ft. Someone)") == "artist"


def test_normalize_artist_strips_ft_inline() -> None:
    """Inline 'ft. X' at end is stripped."""
    assert normalize_artist("Artist ft. Someone") == "artist"


def test_normalize_artist_strips_feat_bracket() -> None:
    """'[feat. X]' bracketed form is stripped."""
    assert normalize_artist("Artist [feat. Someone]") == "artist"


def test_normalize_artist_feat_and_article_combined() -> None:
    """Feat. suffix and leading article are both stripped correctly (FR19)."""
    # "The Beatles feat. Yoko Ono" → feat strip → "The Beatles" → article → "beatles"
    assert normalize_artist("The Beatles feat. Yoko Ono") == "beatles"


def test_normalize_artist_no_feat_unchanged() -> None:
    """Artist without feat. suffix is unaffected."""
    assert normalize_artist("The Police") == "police"


# ---------------------------------------------------------------------------
# FR12: Regression / negative cases
# ---------------------------------------------------------------------------


def test_normalize_artist_acdc() -> None:
    """AC/DC slash is normalized to space (not a destructive split trigger)."""
    assert normalize_artist("AC/DC") == "ac dc"


def test_normalize_artist_earth_wind_fire() -> None:
    """'Earth, Wind & Fire' normalizes with & expanded to 'and' (FR 2.3 change).

    The & → and substitution (Task 3, story 2.3) aligns signatures with
    Airwave for cross-system MusicBrainz matching.  The name is preserved
    as a single token; 'and' replaces '&'.
    """
    result = normalize_artist("Earth, Wind & Fire")
    assert result == "earth wind and fire"


def test_normalize_title_yesterday_live_remaster() -> None:
    """'Yesterday - Live (1994 Remaster)' preserves both base words."""
    result = normalize_title("Yesterday - Live (1994 Remaster)")
    # Remaster year removed; "yesterday" and "live" should both be present
    assert "yesterday" in result
    assert "live" in result


# ---------------------------------------------------------------------------
# FR20: split_artist_string
# ---------------------------------------------------------------------------


def test_split_artist_simple_ampersand() -> None:
    """Split on ' & ' when no comma present in string."""
    result = split_artist_string("Beatles & Rolling Stones")
    assert "beatles" in result
    assert "rolling stones" in result
    assert len(result) == 2


def test_split_artist_slash() -> None:
    """Split on ' / ' (spaced slash)."""
    result = split_artist_string("Artist / Other")
    assert "artist" in result
    assert "other" in result


def test_split_artist_feat() -> None:
    """Split on ' feat. '."""
    result = split_artist_string("Artist feat. Singer")
    assert "artist" in result
    assert "singer" in result


def test_split_artist_ft() -> None:
    """Split on ' ft. '."""
    result = split_artist_string("Artist ft. Singer")
    assert "artist" in result
    assert "singer" in result


def test_split_artist_vs() -> None:
    """Split on ' vs. '."""
    result = split_artist_string("Team A vs. Team B")
    assert len(result) == 2


def test_split_artist_w_slash() -> None:
    """Split on ' w/ '."""
    result = split_artist_string("DJ Name w/ MC Name")
    assert len(result) == 2


def test_split_artist_deduplication() -> None:
    """Duplicate normalized names are deduplicated."""
    result = split_artist_string("The Beatles & Beatles")
    # Both normalize to "beatles"
    assert result == ["beatles"]


def test_split_artist_earth_wind_fire_known_limitation() -> None:
    """'Earth, Wind & Fire' splits into 3 tokens — known limitation (FR20).

    The digit-aware comma split fires on both commas (neither flanked by
    digits), yielding ["Earth", "Wind & Fire"].  Secondary split on '&'
    then yields ["Earth", "Wind", "Fire"] → ["earth", "fire", "wind"].

    This is the same failure as the legacy Airwave code.  Correct handling
    requires an allowlist or DB lookup and is deferred to Epic 3.
    The old blunt-comma heuristic also failed this case; the new behaviour
    is documented here rather than treated as a regression.
    """
    result = split_artist_string("Earth, Wind & Fire")
    # Three separate tokens — wrong, but the known-limitation outcome
    assert result == ["earth", "fire", "wind"]


def test_split_artist_comma_separated_dual_now_splits() -> None:
    """'Artist A, Artist B' now correctly splits on the non-digit comma (FR20).

    The digit-aware comma split (story 2.3 Task 6) replaces the old blunt
    heuristic that blocked ALL secondary splitting when any comma was present.
    """
    result = split_artist_string("Artist A, Artist B")
    assert result == ["artist a", "artist b"]


def test_split_artist_acdc_single() -> None:
    """'AC/DC' (no spaces around slash) is treated as single artist."""
    result = split_artist_string("AC/DC")
    assert result == ["ac dc"]


def test_split_artist_single_name() -> None:
    """Single artist with no separators returns a single-element list."""
    result = split_artist_string("Pink Floyd")
    assert result == ["pink floyd"]


def test_split_artist_empty() -> None:
    """Empty string returns empty list."""
    result = split_artist_string("")
    assert result == []


def test_split_artist_strips_articles_per_token() -> None:
    """Each split token is normalized (articles stripped, etc.)."""
    result = split_artist_string("The Beatles & The Rolling Stones")
    assert "beatles" in result
    assert "rolling stones" in result
    assert "the beatles" not in result


# ---------------------------------------------------------------------------
# FR22: classify_version_descriptor
# ---------------------------------------------------------------------------


def test_classify_live() -> None:
    """Live keywords map to LIVE."""
    assert classify_version_descriptor("Live") == VersionType.LIVE
    assert classify_version_descriptor("Live at Wembley") == VersionType.LIVE
    assert classify_version_descriptor("in concert") == VersionType.LIVE


def test_classify_acoustic() -> None:
    """Acoustic keywords map to ACOUSTIC."""
    assert classify_version_descriptor("Acoustic") == VersionType.ACOUSTIC
    assert classify_version_descriptor("Unplugged") == VersionType.ACOUSTIC


def test_classify_remix() -> None:
    """Remix/mix keywords map to REMIX."""
    assert classify_version_descriptor("Remix") == VersionType.REMIX
    assert classify_version_descriptor("Club Mix") == VersionType.REMIX
    assert classify_version_descriptor("Extended Mix") == VersionType.REMIX
    assert classify_version_descriptor("Dub") == VersionType.REMIX
    # Bare "Mix" alone — uses word-boundary guard \bmix\b after M2 fix (L7)
    assert classify_version_descriptor("Mix") == VersionType.REMIX


def test_classify_remaster() -> None:
    """Remaster keywords map to REMASTER."""
    assert classify_version_descriptor("Remastered") == VersionType.REMASTER
    assert classify_version_descriptor("2023 Remaster") == VersionType.REMASTER
    assert classify_version_descriptor("HD Remaster") == VersionType.REMASTER


def test_classify_radio_edit() -> None:
    """Radio edit / single version keywords map to RADIO_EDIT."""
    assert classify_version_descriptor("Radio Edit") == VersionType.RADIO_EDIT
    assert classify_version_descriptor("Radio Version") == VersionType.RADIO_EDIT
    assert classify_version_descriptor("Single Version") == VersionType.RADIO_EDIT
    assert classify_version_descriptor("Edit") == VersionType.RADIO_EDIT


def test_classify_demo() -> None:
    """Demo keywords map to DEMO."""
    assert classify_version_descriptor("Demo") == VersionType.DEMO
    assert classify_version_descriptor("Rough Mix") == VersionType.DEMO


def test_classify_extended() -> None:
    """Extended keywords map to EXTENDED."""
    assert classify_version_descriptor("Extended") == VersionType.EXTENDED
    assert classify_version_descriptor("Long Version") == VersionType.EXTENDED
    assert classify_version_descriptor("Full Version") == VersionType.EXTENDED


def test_classify_instrumental() -> None:
    """Instrumental keywords map to INSTRUMENTAL."""
    assert classify_version_descriptor("Instrumental") == VersionType.INSTRUMENTAL
    assert classify_version_descriptor("Backing Track") == VersionType.INSTRUMENTAL


def test_classify_original() -> None:
    """Original keywords map to ORIGINAL."""
    assert classify_version_descriptor("Original") == VersionType.ORIGINAL
    assert classify_version_descriptor("Original Recording") == VersionType.ORIGINAL
    assert classify_version_descriptor("Original Version") == VersionType.ORIGINAL


def test_classify_unknown() -> None:
    """Unrecognized descriptor returns UNKNOWN."""
    assert classify_version_descriptor("Something Random") == VersionType.UNKNOWN
    assert classify_version_descriptor("") == VersionType.UNKNOWN


def test_classify_case_insensitive() -> None:
    """Classification is case-insensitive."""
    assert classify_version_descriptor("LIVE") == VersionType.LIVE
    assert classify_version_descriptor("acoustic") == VersionType.ACOUSTIC
    assert classify_version_descriptor("rEmIx") == VersionType.REMIX


# ---------------------------------------------------------------------------
# FR21 / FR23: extract_version_tags
# ---------------------------------------------------------------------------


def test_extract_version_tags_live_paren() -> None:
    """Single version tag in parentheses is extracted."""
    base, tags = extract_version_tags("Song (Live)")
    assert base.strip() == "Song"
    assert tags == ["Live"]


def test_extract_version_tags_bracket() -> None:
    """Version tag in brackets is extracted (strengthened assertions — R5-L1)."""
    base, tags = extract_version_tags("Song [Acoustic Version]")
    assert base.strip() == "Song"
    assert tags == ["Acoustic Version"]


def test_extract_version_tags_mismatched_bracket_not_extracted() -> None:
    """Mismatched bracket pair '(Live]' is NOT extracted (L2 regression guard).

    The strict paired-bracket regex (?:(...)|(...)|(\\[...\\])) must not match
    cross-bracket pairs.  If the regex were reverted to [\\)\\]], this fails.
    """
    base, tags = extract_version_tags("Song (Live]")
    assert tags == []
    assert "Song (Live]" in base


def test_extract_version_tags_multiple() -> None:
    """Multiple version tags are all extracted (FR23)."""
    base, tags = extract_version_tags("Song (Live) (Radio Edit)")
    assert "Song" in base
    assert len(tags) == 2


def test_extract_version_tags_part_number_ignored() -> None:
    """Part numbers are NOT extracted as version tags (FR23)."""
    base, tags = extract_version_tags("Shine On You Crazy Diamond (Part 1)")
    # Part 1 stays in title
    assert "Part 1" in base
    assert tags == []


def test_extract_version_tags_subtitle_kept() -> None:
    """Parenthetical with 8+ words treated as subtitle and kept (FR23)."""
    base, tags = extract_version_tags("Yesterday (Because I Really Truly Told You So Today)")
    assert "Because I Really Truly Told You So Today" in base
    assert tags == []


def test_extract_version_tags_four_word_unknown_kept() -> None:
    """4-word UNKNOWN parenthetical is kept because classify returns UNKNOWN.

    _SUBTITLE_WORD_THRESHOLD = 8; a 4-word group (4 < 8) is NOT blocked by
    the word-count guard.  It is kept because classify_version_descriptor
    returns UNKNOWN — no known version keyword is present.
    """
    base, tags = extract_version_tags("Song (One Two Three Four)")
    assert "One Two Three Four" in base
    assert tags == []


def test_extract_version_tags_bracket_subtitle_kept() -> None:
    """Bracket-style subtitle with 8+ words is kept, not extracted (L6)."""
    base, tags = extract_version_tags("Song [Because I Really Truly Told You So Today]")
    assert "Because I Really Truly Told You So Today" in base
    assert tags == []


def test_extract_version_tags_no_parens() -> None:
    """Title with no parentheticals returns unchanged title and empty list."""
    base, tags = extract_version_tags("Plain Song Title")
    assert base == "Plain Song Title"
    assert tags == []


def test_extract_version_tags_remaster_removed() -> None:
    """Remaster tag is extracted correctly."""
    base, tags = extract_version_tags("Song (Remastered 2023)")
    assert "Song" in base
    assert len(tags) == 1


# ---------------------------------------------------------------------------
# FR24: extract_dash_version
# ---------------------------------------------------------------------------


def test_extract_dash_version_live() -> None:
    """'Song - Live Version' → base + descriptor."""
    base, desc = extract_dash_version("Song - Live Version")
    assert "Song" in base
    assert desc is not None
    assert "live" in desc.lower()


def test_extract_dash_version_radio_edit() -> None:
    """'Song - Radio Edit' → extracted."""
    base, desc = extract_dash_version("Song - Radio Edit")
    assert "Song" in base
    assert desc is not None


def test_extract_dash_version_non_version_not_extracted() -> None:
    """Dash separating non-version text is NOT extracted."""
    base, desc = extract_dash_version("AC/DC - Back In Black")
    assert desc is None
    assert base == "AC/DC - Back In Black"


def test_extract_dash_version_no_dash() -> None:
    """Title with no dash returns original and None."""
    base, desc = extract_dash_version("Plain Song")
    assert base == "Plain Song"
    assert desc is None


def test_extract_dash_version_remix() -> None:
    """'Song - Acoustic Mix' → extracted (recognized version)."""
    base, desc = extract_dash_version("Song - Acoustic Mix")
    assert desc is not None


# ---------------------------------------------------------------------------
# FR25: detect_embedded_remix
# ---------------------------------------------------------------------------


def test_detect_embedded_remix_trailing_remix() -> None:
    """Trailing 'Remix' without delimiter is detected."""
    base, desc = detect_embedded_remix("Song Remix")
    assert desc is not None
    assert "remix" in desc.lower()
    assert "Song" in base


def test_detect_embedded_remix_extended_mix() -> None:
    """Trailing 'Extended Mix' is detected."""
    base, desc = detect_embedded_remix("Song Extended Mix")
    assert desc is not None


def test_detect_embedded_remix_club_mix() -> None:
    """Trailing 'Club Mix' is detected."""
    base, desc = detect_embedded_remix("Song Club Mix")
    assert desc is not None


def test_detect_embedded_remix_dub_mix() -> None:
    """Trailing 'Dub Mix' is detected (M3 — pattern coverage guard)."""
    base, desc = detect_embedded_remix("Song Dub Mix")
    assert desc is not None
    assert "Song" in base


def test_detect_embedded_remix_instrumental() -> None:
    """Trailing 'Instrumental' is detected."""
    base, desc = detect_embedded_remix("Song Instrumental")
    assert desc is not None


def test_detect_embedded_remix_no_match() -> None:
    """Regular title without trailing remix keyword returns None."""
    base, desc = detect_embedded_remix("Yesterday")
    assert base == "Yesterday"
    assert desc is None


def test_detect_embedded_remix_prefix_not_matched() -> None:
    """'Remix of Song' does NOT trigger extraction (only trailing keywords match)."""
    base, desc = detect_embedded_remix("Remix of Song")
    assert desc is None
    assert base == "Remix of Song"


# ---------------------------------------------------------------------------
# FR22 (2.3): New VersionType members — EXPLICIT / COVER / EDITION / ALTERNATE / FORMAT
# ---------------------------------------------------------------------------


def test_classify_explicit() -> None:
    """Explicit/Clean/Lyrical descriptors map to EXPLICIT (FR22)."""
    assert classify_version_descriptor("Explicit") == VersionType.EXPLICIT
    assert classify_version_descriptor("Clean") == VersionType.EXPLICIT
    assert classify_version_descriptor("Lyrical") == VersionType.EXPLICIT
    assert classify_version_descriptor("EXPLICIT") == VersionType.EXPLICIT


def test_classify_cover() -> None:
    """Cover/Cover Version descriptors map to COVER (FR22)."""
    assert classify_version_descriptor("Cover") == VersionType.COVER
    assert classify_version_descriptor("Cover Version") == VersionType.COVER
    assert classify_version_descriptor("cover version") == VersionType.COVER


def test_classify_edition() -> None:
    """Edition-family descriptors map to EDITION (FR22)."""
    assert classify_version_descriptor("Deluxe") == VersionType.EDITION
    assert classify_version_descriptor("Deluxe Edition") == VersionType.EDITION
    assert classify_version_descriptor("Bonus") == VersionType.EDITION
    assert classify_version_descriptor("Anniversary") == VersionType.EDITION
    assert classify_version_descriptor("Anniversary Edition") == VersionType.EDITION
    assert classify_version_descriptor("Special Edition") == VersionType.EDITION
    assert classify_version_descriptor("Limited Edition") == VersionType.EDITION
    # "Edition" by itself must NOT fall into RADIO_EDIT ("edit" ⊂ "edition")
    assert classify_version_descriptor("Edition") == VersionType.EDITION


def test_classify_alternate() -> None:
    """Alt/Alternate/Alternative descriptors map to ALTERNATE (FR22)."""
    assert classify_version_descriptor("Alt") == VersionType.ALTERNATE
    assert classify_version_descriptor("Alt Version") == VersionType.ALTERNATE
    assert classify_version_descriptor("Alternate") == VersionType.ALTERNATE
    assert classify_version_descriptor("Alternative") == VersionType.ALTERNATE


def test_classify_format() -> None:
    """Mono/Stereo descriptors map to FORMAT (FR22)."""
    assert classify_version_descriptor("Mono") == VersionType.FORMAT
    assert classify_version_descriptor("Stereo") == VersionType.FORMAT
    assert classify_version_descriptor("MONO") == VersionType.FORMAT


def test_extract_version_tags_explicit() -> None:
    """'Song (Explicit)' extracts Explicit tag (FR22)."""
    base, tags = extract_version_tags("Song (Explicit)")
    assert base.strip() == "Song"
    assert tags == ["Explicit"]


def test_extract_version_tags_cover() -> None:
    """'Song (Cover)' extracts Cover tag (FR22)."""
    base, tags = extract_version_tags("Song (Cover)")
    assert base.strip() == "Song"
    assert tags == ["Cover"]


def test_extract_version_tags_edition() -> None:
    """'Song (Deluxe Edition)' extracts Edition tag (FR22)."""
    base, tags = extract_version_tags("Song (Deluxe Edition)")
    assert base.strip() == "Song"
    assert tags == ["Deluxe Edition"]


def test_extract_version_tags_alternate() -> None:
    """'Song (Alt)' extracts Alternate tag (FR22)."""
    base, tags = extract_version_tags("Song (Alt)")
    assert base.strip() == "Song"
    assert tags == ["Alt"]


def test_extract_version_tags_format() -> None:
    """'Song (Mono)' extracts Format tag (FR22)."""
    base, tags = extract_version_tags("Song (Mono)")
    assert base.strip() == "Song"
    assert tags == ["Mono"]


# ---------------------------------------------------------------------------
# FR14 (2.3): NFKD decomposition — full-width ASCII and ligatures
# ---------------------------------------------------------------------------


def test_normalize_artist_full_width_unicode() -> None:
    """Full-width ASCII chars (U+FF21 range) are collapsed to ASCII (FR14 NFKD)."""
    # U+FF21 = Ａ (full-width A); NFKD decomposes to ASCII A
    assert normalize_artist("\uff21rtist") == "artist"
    assert normalize_artist("\uff21\uff42\uff43") == "abc"


def test_normalize_title_ligature_unicode() -> None:
    """Ligatures (U+FB01 ﬁ) are decomposed to constituent letters (FR14 NFKD)."""
    # U+FB01 = ﬁ ligature; NFKD decomposes to 'fi'
    assert normalize_title("\ufb01lm score") == "film score"


def test_normalize_artist_nfkd_accent_regression() -> None:
    """Existing accent tests still pass under NFKD (NFD accents ⊆ NFKD accents)."""
    assert normalize_artist("Café") == "cafe"
    assert normalize_artist("Björk") == "bjork"
    assert normalize_artist("Zürich") == "zurich"


# ---------------------------------------------------------------------------
# FR12 (2.3): & → and and + → plus substitution (cross-system compatibility)
# ---------------------------------------------------------------------------


def test_normalize_artist_ampersand_expanded() -> None:
    """& is expanded to 'and' before punctuation removal (story 2.3 Task 3).

    Cross-system implication: signatures now align with Airwave-generated
    signatures that spell out 'and', required for MusicBrainz matching (Epic 3).
    """
    assert normalize_artist("Rock & Roll Band") == "rock and roll band"
    assert normalize_artist("AC&DC") == "ac and dc"


def test_normalize_title_ampersand_expanded() -> None:
    """& is expanded to 'and' in title normalization (story 2.3 Task 3)."""
    assert normalize_title("Rock & Roll") == "rock and roll"


def test_normalize_title_plus_expanded() -> None:
    """+ is expanded to 'plus' before punctuation removal (story 2.3 Task 3).

    Cross-system implication: same as & → and change.
    """
    assert normalize_title("A+B") == "a plus b"
    assert normalize_artist("A+B") == "a plus b"


def test_split_artist_ampersand_still_splits() -> None:
    """split_artist_string still splits on & in raw string (no double-processing).

    & in the raw input is the split character at the separator-detection stage
    (via _SECONDARY_SEP_RE).  Each token is then independently passed through
    normalize_artist(), which applies the & → and substitution only to the
    already-split sub-strings.  There is no double-processing issue.
    """
    result = split_artist_string("Beatles & Stones")
    assert result == ["beatles", "stones"]


# ---------------------------------------------------------------------------
# FR13 (2.3): Bare trailing ellipsis removal
# ---------------------------------------------------------------------------


def test_normalize_title_trailing_ascii_ellipsis() -> None:
    """Trailing '...' (3+ ASCII dots) are removed from title end (FR13)."""
    assert normalize_title("Yesterday...") == "yesterday"
    assert normalize_title("A Long Song Title...") == "a long song title"


def test_normalize_title_trailing_ascii_ellipsis_with_spaces() -> None:
    """Trailing '...' with surrounding whitespace is stripped cleanly."""
    assert normalize_title("Song ...") == "song"


def test_normalize_title_bare_unicode_ellipsis_at_end() -> None:
    """Bare unicode ellipsis (U+2026) at string end is removed (FR13)."""
    assert normalize_title("Song \u2026") == "song"


def test_normalize_title_bare_unicode_ellipsis_mid_string() -> None:
    """Bare unicode ellipsis (U+2026) mid-string collapses to space."""
    # "Song… (something)" — the ellipsis collapses; parens removed by pipeline
    result = normalize_title("Song\u2026 something")
    assert result == "song something"


def test_normalize_title_bracketed_ellipsis_still_removed() -> None:
    """Bracketed '(...)' form continues to be removed — no regression."""
    assert normalize_title("Song (...)") == "song"
    assert normalize_title("Song [...]") == "song"


def test_normalize_artist_truncation_regression() -> None:
    """Existing bracketed truncation tests still pass after FR13 changes."""
    assert "..." not in normalize_artist("Artist (...)")
    assert normalize_artist("Name [\u2026]") == "name"


def test_normalize_title_two_dots_not_stripped() -> None:
    """Two trailing dots (..) are NOT stripped — only 3+ triggers removal."""
    # This is an edge case; the threshold is 3+ dots
    result = normalize_title("Song..")
    # After [^\w\s] removal, dots vanish anyway — but via a different path
    assert "song" in result


def test_normalize_title_unicode_ellipsis_before_parenthetical() -> None:
    """U+2026 immediately before a parenthetical group is collapsed (AC#3 edge case).

    AC#3: "Song… (something)" — the bare ellipsis is replaced with a space by
    step 0a of _remove_remaster_year_truncation; the parenthetical content is
    then stripped by _normalize_special_chars_and_whitespace, leaving "song something".
    """
    assert normalize_title("Song\u2026 (something)") == "song something"


# ---------------------------------------------------------------------------
# FR20 (2.3): New primary separators — with / duet / F/ / W/
# ---------------------------------------------------------------------------


def test_split_artist_with_separator() -> None:
    """'Elton John with Kiki Dee' splits on 'with' (FR20)."""
    result = split_artist_string("Elton John with Kiki Dee")
    assert "elton john" in result
    assert "kiki dee" in result
    assert len(result) == 2


def test_split_artist_duet_with_separator() -> None:
    """'Dean Martin duet with Frank Sinatra' splits on 'duet with' (FR20)."""
    result = split_artist_string("Dean Martin duet with Frank Sinatra")
    assert "dean martin" in result
    assert "frank sinatra" in result
    assert len(result) == 2


def test_split_artist_duet_separator() -> None:
    """'2Pac duet Biggie' splits on bare 'duet' (FR20)."""
    result = split_artist_string("2Pac duet Biggie")
    assert "2pac" in result
    assert "biggie" in result
    assert len(result) == 2


def test_split_artist_f_slash_separator() -> None:
    """'KORN F/ SKRILLEX' splits on 'F/' shorthand (FR20)."""
    result = split_artist_string("KORN F/ SKRILLEX")
    assert "korn" in result
    assert "skrillex" in result
    assert len(result) == 2


def test_split_artist_w_slash_uppercase_separator() -> None:
    """'Artist W/ Guest' splits on uppercase 'W/' shorthand (FR20)."""
    result = split_artist_string("Artist W/ Guest")
    assert "artist" in result
    assert "guest" in result
    assert len(result) == 2


def test_split_artist_w_slash_lowercase_no_regression() -> None:
    """Existing lowercase 'w/' separator still works after pattern update."""
    result = split_artist_string("DJ Name w/ MC Name")
    assert len(result) == 2


def test_split_artist_duet_with_before_with_match() -> None:
    """'duet with' is matched as a unit (longer pattern wins over 'with')."""
    # If 'with' fired first, result would be ["2pac duet", "frank sinatra"]
    result = split_artist_string("2Pac duet with Frank Sinatra")
    assert "2pac" in result
    assert "frank sinatra" in result
    assert len(result) == 2


# ---------------------------------------------------------------------------
# FR20 (2.3): Digit-aware comma splitting
# ---------------------------------------------------------------------------


def test_split_artist_digit_comma_not_split() -> None:
    """Comma between digits is NOT split — '10,000 Maniacs' stays intact (FR20)."""
    result = split_artist_string("10,000 Maniacs & R.E.M.")
    assert "10000 maniacs" in result
    assert "rem" in result
    assert len(result) == 2


def test_split_artist_simon_garfunkel_unchanged() -> None:
    """'Simon & Garfunkel' (no comma) still splits on & as before (FR20)."""
    result = split_artist_string("Simon & Garfunkel")
    assert result == ["garfunkel", "simon"]


def test_split_artist_crosby_stills_nash_young() -> None:
    """'Crosby, Stills, Nash & Young' → 4 tokens via digit-aware comma (FR20).

    Algorithm:
      1. Primary separators: no match → single token
      2. Non-digit commas split: ["Crosby", "Stills", "Nash & Young"]
      3. Secondary on "Nash & Young": ["Nash", "Young"]
      4. Normalise + sort → ["crosby", "nash", "stills", "young"]
    """
    result = split_artist_string("Crosby, Stills, Nash & Young")
    assert result == ["crosby", "nash", "stills", "young"]


# ---------------------------------------------------------------------------
# FR23 (2.3): Roman numeral part-number guard in extract_version_tags
# ---------------------------------------------------------------------------


def test_extract_version_tags_part_roman_ii_not_extracted() -> None:
    """'(Part II)' is NOT extracted as a version tag (FR23 roman guard)."""
    base, tags = extract_version_tags("The Unforgiven (Part II)")
    assert "Part II" in base
    assert tags == []


def test_extract_version_tags_part_roman_iii_not_extracted() -> None:
    """'(Part III)' is NOT extracted as a version tag (FR23 roman guard)."""
    base, tags = extract_version_tags("The Unforgiven (Part III)")
    assert "Part III" in base
    assert tags == []


def test_extract_version_tags_pt_roman_not_extracted() -> None:
    """'(Pt. IV)' abbreviated form is NOT extracted as a version tag (FR23)."""
    base, tags = extract_version_tags("Symphony (Pt. IV)")
    assert "Pt. IV" in base
    assert tags == []


def test_extract_version_tags_part_decimal_still_works() -> None:
    """'(Part 1)' decimal form still passes through unchanged — no regression."""
    base, tags = extract_version_tags("Song (Part 1)")
    assert "Part 1" in base
    assert tags == []


def test_metallica_trilogy_distinct_normalized_signatures() -> None:
    """All three Metallica Unforgiven titles produce distinct signatures.

    Confirms that roman-numeral part parentheticals are preserved as part
    of the base title rather than stripped, so each song has a unique
    normalized_signature for matching purposes.
    """
    sig1 = normalize_title("The Unforgiven")
    sig2 = normalize_title("The Unforgiven II")
    sig3 = normalize_title("The Unforgiven (Part II)")
    assert sig1 != sig2
    assert sig1 != sig3
    assert sig2 != sig3


def test_extract_version_tags_special_edition_full_pipeline() -> None:
    """'Song (Special Edition)' — tag extracted via pipeline (FR22)."""
    base, tags = extract_version_tags("Song (Special Edition)")
    assert base.strip() == "Song"
    assert tags == ["Special Edition"]


def test_split_artist_existing_separators_no_regression() -> None:
    """All pre-2.3 primary separators still work after pattern extension."""
    assert len(split_artist_string("Artist feat. Singer")) == 2
    assert len(split_artist_string("Artist ft. Singer")) == 2
    assert len(split_artist_string("Team A vs. Team B")) == 2
    assert len(split_artist_string("Artist / Other")) == 2
    assert len(split_artist_string("DJ w/ MC")) == 2


def test_split_artist_acdc_single_no_regression() -> None:
    """'AC/DC' (unspaced slash) is still treated as a single artist."""
    assert split_artist_string("AC/DC") == ["ac dc"]


def test_extract_version_tags_all_new_types_extractable() -> None:
    """Comprehensive check — all five new VersionType descriptors are extracted."""
    cases = [
        ("Song (Clean)", "Song", "Clean"),
        ("Song (Alt Version)", "Song", "Alt Version"),
        ("Song (Stereo)", "Song", "Stereo"),
        ("Song (Bonus)", "Song", "Bonus"),
        ("Song (Anniversary Edition)", "Song", "Anniversary Edition"),
    ]
    for raw, expected_base, expected_tag in cases:
        base, tags = extract_version_tags(raw)
        assert base.strip() == expected_base, f"Base mismatch for {raw!r}"
        assert tags == [expected_tag], f"Tag mismatch for {raw!r}"


def test_classify_edition_not_radio_edit_regression() -> None:
    """'Edit' (no -ion suffix) still maps to RADIO_EDIT — no regression."""
    assert classify_version_descriptor("Edit") == VersionType.RADIO_EDIT
    assert classify_version_descriptor("Radio Edit") == VersionType.RADIO_EDIT


def test_extract_version_tags_part_mix_not_guarded() -> None:
    """'(Part MIX)' is NOT guarded by _PART_NUMBER_RE and extracts as REMIX (F2 fix).

    Prior to this fix, M, I, X are all in [IVXLCDM] so the pattern matched
    "Part MIX" as a roman-numeral part number, silently swallowing the REMIX
    descriptor.  After constraining the charset to [IVXLC] (dropping M/D),
    "Part MIX" is correctly classified.
    """
    base, tags = extract_version_tags("Song (Part MIX)")
    # Now correctly extracted — the guard no longer fires on "Part MIX"
    assert tags == ["Part MIX"]
    assert base.strip() == "Song"


def test_classify_version_descriptor_special_word_boundary() -> None:
    """Word-boundary guards for 'special'/'limited' prevent substring false positives.

    Without the guard: classify_version_descriptor("Unlimited") returns EDITION.
    With re.search word-boundary checks the substrings only match as whole words.
    """
    # False positives prevented
    assert classify_version_descriptor("Unlimited") == VersionType.UNKNOWN
    assert classify_version_descriptor("Specialists") == VersionType.UNKNOWN
    # True positives still work
    assert classify_version_descriptor("Special") == VersionType.EDITION
    assert classify_version_descriptor("Limited") == VersionType.EDITION
    assert classify_version_descriptor("Special Edition") == VersionType.EDITION
    assert classify_version_descriptor("Limited Edition") == VersionType.EDITION


def test_classify_version_descriptor_lyrical() -> None:
    """'Lyrical' keyword maps to EXPLICIT (AC#6 coverage)."""
    assert classify_version_descriptor("Lyrical") == VersionType.EXPLICIT
    assert extract_version_tags("Song (Lyrical)")[1] == ["Lyrical"]


# ---------------------------------------------------------------------------
# FR22 (M2 fix): Word-boundary guards — substring false positives prevented
# ---------------------------------------------------------------------------


def test_classify_alive_not_live() -> None:
    """'Alive' must NOT match as LIVE — 'live' is a substring, not a word (M2 fix)."""
    assert classify_version_descriptor("Alive") == VersionType.UNKNOWN


def test_classify_concerto_not_live() -> None:
    """'Concerto' must NOT match as LIVE — 'concert' is a prefix (M2 fix)."""
    assert classify_version_descriptor("Concerto") == VersionType.UNKNOWN
    assert classify_version_descriptor("Piano Concerto") == VersionType.UNKNOWN


def test_classify_credit_not_radio_edit() -> None:
    """'Credit' must NOT match as RADIO_EDIT — 'edit' is a suffix (M2 fix)."""
    assert classify_version_descriptor("Credit") == VersionType.UNKNOWN


def test_classify_mixtape_not_remix() -> None:
    """'Mixtape' must NOT match as REMIX — 'mix' is a prefix (M2 fix)."""
    assert classify_version_descriptor("Mixtape") == VersionType.UNKNOWN


# ---------------------------------------------------------------------------
# FR21 / FR23 (L4 fix): 3-word UNKNOWN parenthetical kept due to UNKNOWN
# ---------------------------------------------------------------------------


def test_extract_version_tags_three_word_unknown_kept_in_title() -> None:
    """3-word parenthetical with no version keyword is kept in base title (L4).

    AC#4: 'Song (It's A Thing)' is kept because classify_version_descriptor
    returns UNKNOWN — NOT because word count exceeds the threshold.
    A 3-word parenthetical with a keyword (e.g. 'Live at Wembley') IS
    extracted.  Guards against accidental reduction of _SUBTITLE_WORD_THRESHOLD.
    """
    base, tags = extract_version_tags("Song (It's A Thing)")
    assert "It's A Thing" in base
    assert tags == []


# ---------------------------------------------------------------------------
# R4 review fixes: word-boundary guards, featuring, whitespace, em-dash
# ---------------------------------------------------------------------------


def test_classify_unremixed_not_remix() -> None:
    """'Unremixed' must NOT match as REMIX — 'remix' is a substring (R4-M1)."""
    assert classify_version_descriptor("Unremixed") == VersionType.UNKNOWN


def test_classify_demolition_not_demo() -> None:
    """'Demolition' must NOT match as DEMO — 'demo' is a prefix (R4-M1)."""
    assert classify_version_descriptor("Demolition") == VersionType.UNKNOWN


def test_classify_monotone_not_format() -> None:
    """'Monotone' must NOT match as FORMAT — 'mono' is a prefix (R4-M1)."""
    assert classify_version_descriptor("Monotone") == VersionType.UNKNOWN


def test_classify_stereophonics_not_format() -> None:
    """'Stereophonics' must NOT match as FORMAT — 'stereo' is a prefix (R4-M1)."""
    assert classify_version_descriptor("Stereophonics") == VersionType.UNKNOWN


def test_normalize_artist_strips_featuring_inline() -> None:
    """Inline 'featuring' (full word) is stripped, not just 'feat.'/'ft.' (R4-M2)."""
    assert normalize_artist("Elton John featuring Kiki Dee") == "elton john"


def test_extract_version_tags_no_double_spaces() -> None:
    """Inner whitespace is collapsed after parenthetical removal (R4-L1).

    A title with extra trailing space around a parenthetical must not leave
    double spaces in the cleaned base title.
    """
    base, tags = extract_version_tags("Song (Live)  After")
    assert base == "Song After"
    assert tags == ["Live"]


def test_extract_dash_version_em_dash() -> None:
    """Em dash (U+2014) is recognised as a dash separator (R4-L2).

    Raw titles are received pre-normalisation so _normalize_smart_quotes has
    not yet converted U+2014 to '-'.  The em-dash form must be handled here.
    """
    base, desc = extract_dash_version("Song \u2014 Live Version")
    assert base == "Song"
    assert desc == "Live Version"


def test_extract_dash_version_en_dash() -> None:
    """En dash (U+2013) is recognised as a dash separator (AC#6).

    Code handles ' - ', ' \u2013 ', ' \u2014 '; only ASCII and em-dash were
    tested previously. En-dash is common in broadcast titles (e.g. "Song – Live").
    """
    base, desc = extract_dash_version("Song \u2013 Radio Edit")
    assert base == "Song"
    assert desc == "Radio Edit"


# ---------------------------------------------------------------------------
# R5 review fixes: featuring paren/bracket, x-separator, bare Dub, dub guard
# ---------------------------------------------------------------------------


def test_normalize_artist_strips_featuring_paren() -> None:
    """'(featuring X)' paren form is stripped — R5-M1 coverage.

    _FEAT_PAREN_RE must include (?:uring)? to match the full word 'featuring',
    consistent with _FEAT_INLINE_RE which was extended in R4-M2.
    """
    assert normalize_artist("Madonna (featuring Justin Timberlake)") == "madonna"


def test_normalize_artist_strips_featuring_bracket() -> None:
    """'[featuring X]' bracket form is stripped — R5-M1 coverage.

    _FEAT_BRACKET_RE must include (?:uring)? symmetrically with the paren
    and inline patterns.
    """
    assert normalize_artist("Artist [featuring Singer]") == "artist"


def test_classify_dubbed_not_remix() -> None:
    """'Dubbed' must NOT match as REMIX — 'dub' is a prefix (R5-L4).

    'dub' in 'dubbed' is True without a word-boundary guard, yielding a
    false REMIX classification.  re.search(r'\\bdub\\b', d) prevents this.
    """
    assert classify_version_descriptor("Dubbed") == VersionType.UNKNOWN


def test_classify_recover_discover_not_cover() -> None:
    """Recover/Discover must NOT match COVER — \\bcover\\b guard (R5-L1)."""
    assert classify_version_descriptor("Recover") == VersionType.UNKNOWN
    assert classify_version_descriptor("Discover") == VersionType.UNKNOWN


def test_classify_acoustics_not_acoustic() -> None:
    """'Acoustics' must NOT match as ACOUSTIC — word-boundary guard (R5-L2)."""
    assert classify_version_descriptor("Acoustics") == VersionType.UNKNOWN


def test_split_artist_x_separator() -> None:
    """'Artist x Other' splits on the 'x' secondary separator (AC#3, R5-L2).

    The 'x' collaboration marker is in _SECONDARY_SEP_RE but was untested.
    """
    result = split_artist_string("Artist x Other")
    assert "artist" in result
    assert "other" in result
    assert len(result) == 2


def test_detect_embedded_remix_dub() -> None:
    """Trailing bare 'Dub' (no compound) is detected (AC#7, R5-L3).

    _EMBEDDED_REMIX_PATTERNS includes r'\\b(Dub)\\s*$' but was only
    exercised indirectly via 'Dub Mix'; this test targets the bare pattern.
    """
    base, desc = detect_embedded_remix("Song Dub")
    assert desc is not None
    assert "Dub" in desc
    assert "Song" in base

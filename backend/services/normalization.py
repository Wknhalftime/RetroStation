"""Normalization pipeline for artist and title strings (FR12–FR25).

Pure functions: no DB imports, no I/O.  Used by ingestion to produce
normalized_name and normalized_signature for LogArtist/LogIdentity
deduplication, and by the matching engine (Epic 3) for version and
artist-split analysis.

Pipeline order for normalize_artist():
    1. _strip_feat_suffix   — remove feat./ft. collaboration suffixes
    2. _normalize_smart_quotes
    3. _strip_accents       — Unicode NFKD, drop combining marks
    4. lowercase
    5. _strip_leading_article — strip "the"/"a"/"an" from start
    6. _remove_remaster_year_truncation
    7. _normalize_special_chars_and_whitespace
"""

import hashlib
import re
import unicodedata

from unidecode import unidecode

from backend.domain.enums import VersionType

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

# Smart quotes and similar to ASCII equivalents
_SMART_QUOTE_MAP: dict[str, str] = {
    "\u2018": "'",  # left single
    "\u2019": "'",  # right single
    "\u201c": '"',  # left double
    "\u201d": '"',  # right double
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2012": "-",  # figure dash
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
}

# Feat. suffix patterns (case-insensitive): (feat. X), [ft. X], feat. X, etc.
_FEAT_PAREN_RE = re.compile(
    r"\s*\(\s*f(?:ea)?t(?:uring)?\.?\s+[^)]+\)",
    re.IGNORECASE,
)
_FEAT_BRACKET_RE = re.compile(
    r"\s*\[\s*f(?:ea)?t(?:uring)?\.?\s+[^\]]+\]",
    re.IGNORECASE,
)
_FEAT_INLINE_RE = re.compile(
    r"\s+f(?:ea)?t(?:uring)?\.?\s+.+$",
    re.IGNORECASE,
)

# Leading-article strip (applied after lowercasing)
_ARTICLE_RE = re.compile(r"^(?:the|an?)\s+")

# Part-number pattern: "Part 1", "Pt. 2", "Pt 3", and roman-numeral forms
# "Part II", "Part III", "Pt. IV".  The roman-numeral branch uses [IVXLC]+
# (case-insensitive via flag), deliberately omitting M and D so that
# broadcast-log descriptors like "Part MIX" (M ∈ [IVXLCDM]) are NOT guarded
# and can be classified normally (→ REMIX).  The effective guard range covers
# I (1) through XCIX (99) and beyond using the available I, V, X, L, C chars.
_PART_NUMBER_RE = re.compile(
    r"^\s*(?:part|pt\.?)\s+(?:\d+|[IVXLC]+)\s*$",
    re.IGNORECASE,
)

# Subtitle word threshold: parentheticals with this many words are kept
_SUBTITLE_WORD_THRESHOLD = 4

# Embedded-remix trailing patterns (ordered longest-first to avoid partial matches)
_EMBEDDED_REMIX_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(Extended Mix)\s*$", re.IGNORECASE),
    re.compile(r"\b(Club Mix)\s*$", re.IGNORECASE),
    re.compile(r"\b(Acoustic Mix)\s*$", re.IGNORECASE),
    re.compile(r"\b(Dub Mix)\s*$", re.IGNORECASE),
    re.compile(r"\b(Instrumental)\s*$", re.IGNORECASE),
    re.compile(r"\b(Remix)\s*$", re.IGNORECASE),
    re.compile(r"\b(Dub)\s*$", re.IGNORECASE),
]

# Artist-string split separators: primary (always split) and secondary
# Primary pattern notes:
#   - duet\s+with must precede 'with' (longer match wins in alternation)
#   - [FW]/ handles F/ and W/ shorthands; w/ is also listed explicitly for
#     clarity (redundant under IGNORECASE but avoids ordering confusion)
#   - \s* trailing (not \s+) allows "F/SKRILLEX" with no space after slash
_PRIMARY_SEP_RE = re.compile(
    r"\s+(?:feat\.|feat|ft\.|ft|vs\.|vs|w/|duet\s+with|with|duet|[FW]/)\s*"
    r"|\s+/\s+",
    re.IGNORECASE,
)
_SECONDARY_SEP_RE = re.compile(r"\s+(?:&|and|x)\s+", re.IGNORECASE)

# Digit-aware comma: split on commas that are NOT between two digit characters.
# (?<!\d)  — the char immediately before the comma is NOT a digit
# (?!\d)   — the char immediately after comma+spaces is NOT a digit
# This preserves numeric band names like "10,000 Maniacs" while still
# splitting "Crosby, Stills, Nash & Young" at the non-numeric commas.
_DIGIT_COMMA_RE: re.Pattern[str] = re.compile(r"(?<!\d),\s*(?!\d)")

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _strip_feat_suffix(text: str) -> str:
    """Remove feat./ft. collaboration suffixes before further normalisation."""
    t = _FEAT_PAREN_RE.sub("", text)
    t = _FEAT_BRACKET_RE.sub("", t)
    t = _FEAT_INLINE_RE.sub("", t)
    return t.strip()


def _normalize_smart_quotes(text: str) -> str:
    """Replace smart/curly quotes and typographic dashes with ASCII equivalents."""
    result = text
    for old, new in _SMART_QUOTE_MAP.items():
        result = result.replace(old, new)
    return result


def _strip_accents(text: str) -> str:
    """Transliterate accents and non-ASCII characters to ASCII equivalents (FR14).

    Uses unidecode for broader coverage than NFKD: handles Cyrillic, CJK,
    Arabic, and other non-Latin scripts by transliterating them to approximate
    ASCII representations.  This is strictly more powerful than NFKD+drop-Mn.
    """
    return unidecode(text)


def _strip_leading_article(text: str) -> str:
    """Strip leading 'the', 'a', or 'an' article from a lowercased string (FR19)."""
    return _ARTICLE_RE.sub("", text)


def _remove_remaster_year_truncation(text: str) -> str:
    """Remove remaster/year/truncation markers (FR13, FR15, FR16).

    Bare ellipsis removal is applied *first* (steps 0a/0b) so that the
    bracketed-form patterns below do not need to re-handle the unicode
    ellipsis character after it has been collapsed to a space.
    """
    # 0a: Bare unicode ellipsis (U+2026) anywhere in string → space
    t = text.replace("\u2026", " ")
    # 0b: Bare 3+ trailing ASCII dots anchored at end of string only.
    #     The $ anchor prevents stripping ellipsis mid-title (e.g. "A...B"
    #     would be affected, but that is an acceptable trade-off for
    #     broadcast log data where trailing truncation is the common pattern).
    t = re.sub(r"\s*\.{3,}\s*$", "", t)
    # Truncation: (...), [...], (…) — (…) is now a no-op but kept for clarity
    t = re.sub(r"\(\s*\.{2,}\s*\)", " ", t)
    t = re.sub(r"\[\s*\.{2,}\s*\]", " ", t)
    t = re.sub(r"\(\s*\u2026\s*\)", " ", t)
    # Remaster / year in parentheses: (Remastered 2023), (2022), (1994)
    t = re.sub(r"\(\s*[Rr]emaster[^)]*\)", " ", t)
    t = re.sub(r"\(\s*\d{4}\s*\)", " ", t)
    t = re.sub(r"\[\s*\d{4}\s*\]", " ", t)
    return t.strip()


def _normalize_special_chars_and_whitespace(text: str) -> str:
    """Normalize punctuation and collapse whitespace.

    ``&`` → ``and`` and ``+`` → ``plus`` are substituted *before* the
    ``[^\\w\\s]`` removal step so the expanded words are preserved in the
    normalized output.  This aligns RetroStation signatures with Airwave
    signatures for cross-system MusicBrainz matching (Epic 3).

    Note: ``split_artist_string`` operates on the *raw* string before calling
    this function per-token, so ``&`` remains a valid split character at the
    separator-detection stage.
    """
    # Replace common special chars with space, then collapse
    t = re.sub(r"[\-_/\\|]", " ", text)
    t = t.replace("&", " and ")  # BEFORE [^\w\s] removal — preserved in output
    t = t.replace("+", " plus ")  # BEFORE [^\w\s] removal — preserved in output
    t = re.sub(r"[^\w\s]", "", t)  # remove remaining punctuation
    t = re.sub(r"\s+", " ", t)
    return t.strip().lower()


# ---------------------------------------------------------------------------
# Public core-pipeline functions
# ---------------------------------------------------------------------------


def normalize_artist(raw: str) -> str:
    """Normalise a raw artist string for deduplication and signature (FR12–FR19).

    Pipeline: feat-strip → smart-quotes → accents → lower →
    article-strip → remaster/year/trunc → special-chars/whitespace.

    Args:
        raw: Original artist string from the broadcast log.

    Returns:
        Normalised lowercase ASCII string, or empty string if input is blank.
    """
    if not raw or not raw.strip():
        return ""
    t = _strip_feat_suffix(raw)
    t = _normalize_smart_quotes(t)
    t = _strip_accents(t)
    t = t.lower()
    t = _strip_leading_article(t)
    t = _remove_remaster_year_truncation(t)
    t = _normalize_special_chars_and_whitespace(t)
    return t


def normalize_title(raw: str) -> str:
    """Normalise a raw title string with the core pipeline (FR12–FR17).

    Does NOT strip version tags — the signature intentionally differs between
    versions (Live vs Studio).  Use extract_version_tags / extract_dash_version
    in the matching engine when version-agnostic comparison is needed.

    Args:
        raw: Original title string from the broadcast log.

    Returns:
        Normalised lowercase ASCII string, or empty string if input is blank.
    """
    if not raw or not raw.strip():
        return ""
    t = _normalize_smart_quotes(raw)
    t = _strip_accents(t)
    t = t.lower()
    t = _remove_remaster_year_truncation(t)
    # Remove standalone years outside parentheses (1950-2029)
    temp_text = _YEAR_PATTERN.sub("", t)
    # Only apply if it doesn't strip the entire title (e.g. "1999" by Prince)
    if temp_text.strip() or not t.strip():
        t = temp_text
    t = _normalize_special_chars_and_whitespace(t)
    return t


def strict_normalize(text: str) -> str:
    """Strip ALL non-alphanumeric chars, collapse whitespace.

    Used as a high-confidence tiebreaker in fuzzy matching — if two
    strict-normalized titles are identical, the match score is 100.
    """
    base = normalize_title(text)
    stripped = re.sub(r"[^a-z0-9 ]", " ", base)
    return re.sub(r" +", " ", stripped).strip()


def compute_normalized_signature(normalized_artist: str, normalized_title: str) -> str:
    """Compute deterministic 32-char hex signature for LogIdentity (FR18).

    MD5 of (normalized_artist + '||' + normalized_title).

    Args:
        normalized_artist: Already-normalised artist string.
        normalized_title: Already-normalised title string.

    Returns:
        32-character lowercase hexadecimal string.
    """
    payload = normalized_artist + "||" + normalized_title
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# FR20: Artist splitting
# ---------------------------------------------------------------------------


def split_artist_string(raw: str) -> list[str]:
    """Split a compound artist string into individually normalised names (FR20).

    Three-step algorithm:
    1. Primary separators (feat., ft., vs., w/, with, duet, F/, W/, spaced /)
       are always applied — unambiguous collaboration markers.
    2. Digit-aware comma splitting: commas NOT flanked by digit characters
       trigger a split.  This preserves numeric names like "10,000 Maniacs"
       while splitting "Crosby, Stills, Nash & Young" at each text comma.
    3. Secondary separators (&, and, x) applied to each remaining token.

    Each token is passed through normalize_artist() (full pipeline including
    article stripping), then the list is deduplicated and sorted.

    This function is a standalone utility for the matching engine.  Ingestion
    still creates one LogArtist per original compound string.

    Args:
        raw: Raw artist string, possibly containing multiple artist names.

    Returns:
        Sorted, deduplicated list of normalised artist name strings.
        Empty list for blank input.
    """
    if not raw or not raw.strip():
        return []

    # Step 1: Split on primary separators (unambiguous collaboration markers)
    tokens: list[str] = _PRIMARY_SEP_RE.split(raw)

    # Step 2: Digit-aware comma splitting — split on commas NOT between digits.
    # This replaces the old "if no comma" heuristic with a more precise check:
    #   "10,000 Maniacs & R.E.M." → comma is between digits → NOT split
    #   "Crosby, Stills, Nash & Young" → commas are between letters → split
    # Known limitation: "Earth, Wind & Fire" is split into three tokens here
    # because the commas are not digit-guarded.  Neither implementation can
    # avoid this without an allowlist.  See test_split_artist_earth_wind_fire*.
    comma_expanded: list[str] = []
    for tok in tokens:
        comma_expanded.extend(_DIGIT_COMMA_RE.split(tok))
    tokens = comma_expanded

    # Step 3: Secondary separators (&, and, x) applied to each token
    expanded: list[str] = []
    for tok in tokens:
        expanded.extend(_SECONDARY_SEP_RE.split(tok))
    tokens = expanded

    # Normalise each token; discard blanks
    normalised = [normalize_artist(tok.strip()) for tok in tokens if tok.strip()]

    # Deduplicate (preserve first occurrence order) then sort
    seen: set[str] = set()
    unique: list[str] = []
    for name in normalised:
        if name and name not in seen:
            seen.add(name)
            unique.append(name)

    return sorted(unique)


# ---------------------------------------------------------------------------
# FR22: Version descriptor classification
# ---------------------------------------------------------------------------


def classify_version_descriptor(descriptor: str) -> VersionType:
    """Classify a version descriptor string into a canonical VersionType (FR22).

    Uses keyword heuristics; multi-word phrases are checked before single
    keywords to avoid false matches (e.g. "Rough Mix" → DEMO, not REMIX).
    Matching is case-insensitive.

    Args:
        descriptor: Raw version descriptor string (e.g. "Live at Wembley").

    Returns:
        Matching VersionType enum member, or VersionType.UNKNOWN.
    """
    d = descriptor.lower().strip()
    if not d:
        return VersionType.UNKNOWN

    # Multi-word phrases first (order matters to avoid partial overlaps)
    if "rough mix" in d:
        return VersionType.DEMO
    if "radio edit" in d or "radio version" in d or "single version" in d:
        return VersionType.RADIO_EDIT
    if "club mix" in d or "extended mix" in d:
        return VersionType.REMIX
    if "long version" in d or "full version" in d:
        return VersionType.EXTENDED
    if "original recording" in d or "original version" in d:
        return VersionType.ORIGINAL
    if "backing track" in d:
        return VersionType.INSTRUMENTAL
    if "in concert" in d:
        return VersionType.LIVE
    # New multi-word phrases (FR22 coverage — checked before single keywords)
    if "cover version" in d:
        return VersionType.COVER
    if (
        "deluxe edition" in d
        or "special edition" in d
        or "limited edition" in d
        or "anniversary edition" in d
    ):
        return VersionType.EDITION
    if "alt version" in d or "alternate version" in d or "alternative version" in d:
        return VersionType.ALTERNATE

    # Single keywords — word-boundary guards prevent substring false positives
    # (e.g. "Alive" ⊃ "live", "Concerto" ⊃ "concert", "Mixtape" ⊃ "mix",
    # "Credit" ⊃ "edit").  Pattern: re.search(r"\bkeyword\b", d) mirrors the
    # existing guards already used for "special" / "limited" / "clean" / "alt".
    if re.search(r"\blive\b", d) or re.search(r"\bconcert\b", d):
        return VersionType.LIVE
    if re.search(r"\bacoustic\b", d) or "unplugged" in d:
        return VersionType.ACOUSTIC
    if (
        re.search(r"\b(?:remix|remixed)\b", d)
        or re.search(r"\bdub\b", d)
        or re.search(r"\bmix\b", d)
    ):
        return VersionType.REMIX
    if "remaster" in d:
        return VersionType.REMASTER
    # "edition" must be checked before "edit" to avoid "Edition" → RADIO_EDIT
    if "edition" in d:
        return VersionType.EDITION
    if re.search(r"\bedit\b", d):
        return VersionType.RADIO_EDIT
    if re.search(r"\bdemo\b", d):
        return VersionType.DEMO
    if "extended" in d:
        return VersionType.EXTENDED
    if "instrumental" in d:
        return VersionType.INSTRUMENTAL
    if "original" in d:
        return VersionType.ORIGINAL
    # New single keywords (FR22 coverage)
    if "explicit" in d or re.search(r"\bclean\b", d) or "lyrical" in d:
        return VersionType.EXPLICIT
    if re.search(r"\bcover\b", d):
        return VersionType.COVER
    if (
        "deluxe" in d
        or "bonus" in d
        or "anniversary" in d
        or re.search(r"\bspecial\b", d)
        or re.search(r"\blimited\b", d)
    ):
        return VersionType.EDITION
    if re.search(r"\balt\b", d) or "alternate" in d or "alternative" in d:
        return VersionType.ALTERNATE
    if re.search(r"\bmono\b", d) or re.search(r"\bstereo\b", d):
        return VersionType.FORMAT

    return VersionType.UNKNOWN


# ---------------------------------------------------------------------------
# FR21 / FR23: Version tag extraction from parentheses/brackets
# ---------------------------------------------------------------------------


def extract_version_tags(raw_title: str) -> tuple[str, list[str]]:
    """Extract version descriptors from parenthetical / bracketed groups (FR21, FR23).

    Scans all ``(...)`` and ``[...]`` groups from right to left.  A group is
    extracted as a version tag only when:

    - It is NOT a part-number (e.g. "Part 1").
    - Its content has fewer than ``_SUBTITLE_WORD_THRESHOLD`` words.
    - ``classify_version_descriptor`` returns a non-UNKNOWN result.

    Args:
        raw_title: Title string to analyse, with original casing preserved.

    Returns:
        Tuple of (cleaned_base_title, list_of_extracted_descriptor_strings).
        Tags are in left-to-right order.  Base title has extracted groups removed.
    """
    # Strict paired-bracket pattern: \( matched only with \), \[ only with \].
    # The previous r"(\s*[\(\[])([^\)\]]+)([\)\]])" had an independent closing
    # charset [\)\]] that allowed mismatched pairs like "(Live]" to match.
    group_re = re.compile(r"\s*(?:\(([^\)]+)\)|\[([^\]]+)\])")
    matches = list(group_re.finditer(raw_title))

    base = raw_title
    tags: list[str] = []

    for match in reversed(matches):
        content = (match.group(1) or match.group(2) or "").strip()

        # Skip part numbers
        if _PART_NUMBER_RE.match(content):
            continue

        # Skip subtitles (too many words to be a version tag)
        if len(content.split()) >= _SUBTITLE_WORD_THRESHOLD:
            continue

        # Only extract known version types
        if classify_version_descriptor(content) != VersionType.UNKNOWN:
            start, end = match.span()
            base = base[:start] + base[end:]
            tags.insert(0, content)

    return re.sub(r"\s+", " ", base).strip(), tags


# ---------------------------------------------------------------------------
# FR24: Dash-separated version extraction
# ---------------------------------------------------------------------------


def extract_dash_version(raw_title: str) -> tuple[str, str | None]:
    """Extract a dash-separated version suffix from a title (FR24).

    Splits on the last `` - `` or `` – `` occurrence; the suffix is extracted
    only if ``classify_version_descriptor`` returns a non-UNKNOWN VersionType.
    This avoids false splits on artist–title formatted strings like
    "AC/DC - Back In Black".

    Args:
        raw_title: Title string with original casing.

    Returns:
        Tuple of (base_title, descriptor_string) when a version suffix is
        found, or (original_title, None) when not.
    """
    for dash in (" - ", " \u2013 ", " \u2014 "):
        idx = raw_title.rfind(dash)
        if idx == -1:
            continue
        potential_suffix = raw_title[idx + len(dash) :]
        if classify_version_descriptor(potential_suffix) != VersionType.UNKNOWN:
            return raw_title[:idx].strip(), potential_suffix
    return raw_title, None


# ---------------------------------------------------------------------------
# FR25: Embedded remix/mix detection
# ---------------------------------------------------------------------------


def detect_embedded_remix(raw_title: str) -> tuple[str, str | None]:
    """Detect trailing remix/mix descriptors lacking explicit delimiters (FR25).

    Checks for known remix/mix keywords anchored at the end of the title
    string.  Only trailing occurrences are extracted; "Remix of Song" is
    unchanged.

    Args:
        raw_title: Title string with original casing.

    Returns:
        Tuple of (base_title_without_suffix, descriptor) when a trailing
        keyword is found, or (original_title, None) when not.
    """
    for pattern in _EMBEDDED_REMIX_PATTERNS:
        m = pattern.search(raw_title)
        if m:
            desc = m.group(1)
            base = raw_title[: m.start()].strip()
            return base, desc
    return raw_title, None


# Standalone year pattern for year-removal guard in normalize_title()
# Covers 1950-2029; intentionally excludes years before 1950 or after 2029
# to avoid stripping numeric-looking titles like "1984" (Orwell) in the
# near future.
_YEAR_PATTERN = re.compile(r"\b(19[5-9]\d|20[0-2]\d)\b")

# ---------------------------------------------------------------------------
# Station call letters (Story 5.4)
# ---------------------------------------------------------------------------

_STATION_SUFFIX_RE = re.compile(r"-(?:FM|AM|TV)$", re.IGNORECASE)


def normalize_station_call_letters(raw: str) -> str:
    """Normalize station call letters so variants like KAZR and KAZR-FM map to one key.

    Strips whitespace, uppercases, removes internal spaces, and drops a trailing
    ``-FM`` / ``-AM`` / ``-TV`` suffix.
    """
    t = raw.strip().upper()
    t = re.sub(r"\s+", "", t)
    t = _STATION_SUFFIX_RE.sub("", t)
    return t

"""Title-scoring policy: how a broadcast title is scored against a library file's title.

Everything that decides *what two strings get compared* and *how well they
match* lives here. The auto-match decision (thresholds, gap, reason codes)
built on top of that score is match policy, not title scoring, and stays in
``identity_matching_service._score_candidates``.

See AUD-014: this module exists so that changing how titles are compared
(guest-credit stripping, bracketed alternate titles, etc.) touches only this
file plus its tests, instead of being split across two files with the same
reason to change.
"""

from __future__ import annotations

import re

from rapidfuzz.fuzz import token_sort_ratio

from backend.domain.library import LibraryFile
from backend.services.normalization import normalize_title, strip_bracketed_groups

_STRIP_SUFFIXES = re.compile(
    r"\s*[\(\[](live|remix|edit|radio edit|acoustic|extended|extended mix|"
    r"remaster(?:ed)?|remastered version|original mix|club mix|instrumental|"
    r"reprise|single version|album version)\s*[\)\]]",
    re.IGNORECASE,
)

_STRIP_FEAT = re.compile(
    r"\s*[\(\[]?(?:feat(?:uring)?|ft)\.?\s+[^\)\]]+[\)\]]?",
    re.IGNORECASE,
)


# Broadcast logs credit guests in shorthand after the title: "Smooth f/Rob
# Thomas", "The First Noel w/Faith Hill". normalize_title turns the slash
# into a space, after which "f rob thomas" can't be told from title words,
# so the credit comes off the raw title. f/ always means featuring.
_F_CREDIT = re.compile(
    r"\s*[\(\[]?\s*(?<![a-z0-9])f/\s*[^\)\]]+[\)\]]?\s*$",
    re.IGNORECASE,
)
# w/ is also "with" inside a title ("Killing Me Softly W/His Song"), and
# w/o and w/out mean "without".
_W_CREDIT = re.compile(
    r"\s*[\(\[]?\s*(?<![a-z0-9])w/(?!o\b|out\b)\s*[^\)\]]+[\)\]]?\s*$",
    re.IGNORECASE,
)


def _credit_stripped_forms(original_title: str) -> list[str]:
    """The raw title with its f/ credit stripped, then also without a trailing
    w/ credit when there is one. Never empty. See broadcast_title_variants."""
    primary = _F_CREDIT.sub("", original_title) or original_title
    forms = [primary]
    without_with = _W_CREDIT.sub("", primary)
    if without_with and without_with != primary:
        forms.append(without_with)
    return forms


def broadcast_title_variants(original_title: str) -> tuple[str, ...]:
    """Normalized forms of a raw broadcast title to score library titles against.

    The first has any f/ credit stripped; it is also the form to search
    MusicBrainz with. When the title ends in a w/ credit, a second form
    strips that too. w/ is as often part of the title itself, so callers
    keep whichever form scores better. A title that is nothing but a
    credit is kept whole.
    """
    return tuple(normalize_title(f) for f in _credit_stripped_forms(original_title))


def broadcast_title_core_variants(original_title: str) -> tuple[str, ...]:
    """The broadcast_title_variants again with their bracketed groups stripped.

    "(Stand By Me)" is an alternate title the library tag may not carry and
    "(Live)" a version it may; scoring the title without them recovers an
    otherwise exact match. Only forms that differ from the full ones are
    returned, so a plain title gives an empty tuple. Part numbers are never
    stripped (see strip_bracketed_groups).
    """
    full = broadcast_title_variants(original_title)
    forms: list[str] = []
    for raw in _credit_stripped_forms(original_title):
        core = normalize_title(strip_bracketed_groups(raw))
        if core and core not in full and core not in forms:
            forms.append(core)
    return tuple(forms)


def library_title_variants(
    track_title: str | None,
    normalized_title: str | None,
) -> tuple[str, ...]:
    """Normalized forms of a library file's title to score a broadcast title against.

    The first is the stored normalized_title (set by library_scan_service via
    normalize_title()), or normalize_title(track_title) for legacy rows that
    pre-date the backfill. Both sides of the comparison must live in that same
    canonical space: token_sort_ratio has no processor, so plain "Halo" vs
    "halo" would score 75. A bracketed group in the tag adds a form without
    it, mirroring broadcast_title_variants.
    """
    full = normalized_title or normalize_title(track_title or "")
    forms = [full]
    if track_title:
        core = normalize_title(strip_bracketed_groups(track_title))
        if core and core != full:
            forms.append(core)
    return tuple(forms)


def normalize_title_for_scoring(title: str) -> str:
    """Strip broadcast/library title to canonical core before fuzzy scoring.

    Removes (Live), (Remix), (Edit), (Radio Edit), (Acoustic), feat. clauses,
    and common variant suffixes. Applied to BOTH sides of every fuzzy comparison.
    Result: "Song Title (Live)" vs "Song Title" -> 100, not 75.
    """
    t = _STRIP_FEAT.sub("", title)
    t = _STRIP_SUFFIXES.sub("", t)
    return t.strip()


def _candidate_titles(f: LibraryFile) -> tuple[str, ...]:
    """The library file's normalized title forms for scoring; see
    library_title_variants for why the stored normalized_title comes first."""
    return library_title_variants(f.audio.track_title, f.audio.normalized_title)


def _candidate_scores(
    full_bcs: list[str],
    core_bcs: list[str],
    f: LibraryFile,
    strong_match_threshold: int,
) -> tuple[float, float]:
    """(score that ranks the candidate, score of the two full forms).

    The full broadcast forms (guest credits stripped) are scored against the
    file's full title. That is the score unless a comparison with bracketed
    groups stripped from either side is itself a strong match, in which case
    that higher score is used: the bracket on one side was noise ("Train In
    Vain (Stand By Me)" against a tag of "Train in Vain"). Below the
    threshold a stripped comparison is ignored, because shortening both
    titles inflates the score of the wrong file as well, and the mid-band
    gap rule would then auto-match it ("Cry Baby Cry" against "Baby It's You
    [Mono]" climbs from 48 to 58).

    The full-form score breaks ties, so a file whose tag carries the same
    bracketed text as the log line beats one that only matches once the
    brackets are stripped ("Hello (Live)" picks the live file over "Hello"
    when both reach 100).
    """
    libs = [normalize_title_for_scoring(t) for t in _candidate_titles(f)]
    exact = float(token_sort_ratio(full_bcs[0], libs[0]))
    full = max(float(token_sort_ratio(bc, libs[0])) for bc in full_bcs)
    stripped = max(float(token_sort_ratio(bc, lib)) for bc in full_bcs + core_bcs for lib in libs)
    return (stripped if stripped >= strong_match_threshold else full), exact

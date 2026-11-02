"""Title-scoring policy: how a broadcast title is scored against a library file's title.

Everything that decides *what two strings get compared* and *how well they
match* lives here. The auto-match decision (thresholds, gap, reason codes)
built on top of that score is match policy, not title scoring, and stays in
``identity_matching_service._score_candidates``.

See AUD-014: this module exists so that changing how titles are compared
(guest-credit stripping, bracketed alternate titles, etc.) touches only this
file plus its tests, instead of being split across two files with the same
reason to change.

It holds two scorers: today's (``_candidate_scores``) and V11.1
(``v11_title_score``), which scores beside it in trial mode (AUD-R100).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import lru_cache

from rapidfuzz.fuzz import ratio, token_sort_ratio

from backend.domain.library import LibraryFile
from backend.services.normalization import (
    extract_dash_version,
    normalize_title,
    strip_bracketed_groups,
    title_part_number,
)

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


@lru_cache(maxsize=65536)
def _scoring_titles(track_title: str | None, normalized_title: str | None) -> tuple[str, ...]:
    """A library file's title forms, ready to score; see library_title_variants for why the
    stored normalized_title comes first.

    Cached by title: a stage scores each of an artist's files against every song of the
    artist, and the forms depend on the titles alone (profile 2026-10-18). The bound holds
    a whole library's titles (33k files on dev).
    """
    return tuple(
        normalize_title_for_scoring(t)
        for t in library_title_variants(track_title, normalized_title)
    )


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
    titles inflates the score of the wrong file as well, lifting it over the
    56% song floor into review ("Cry Baby Cry" against "Baby It's You
    [Mono]" climbs from 48 to 58).

    The full-form score breaks ties, so a file whose tag carries the same
    bracketed text as the log line beats one that only matches once the
    brackets are stripped ("Hello (Live)" picks the live file over "Hello"
    when both reach 100).
    """
    libs = _scoring_titles(f.audio.track_title, f.audio.normalized_title)
    exact = float(token_sort_ratio(full_bcs[0], libs[0]))
    full = max(float(token_sort_ratio(bc, libs[0])) for bc in full_bcs)
    stripped = max(float(token_sort_ratio(bc, lib)) for bc in full_bcs + core_bcs for lib in libs)
    return (stripped if stripped >= strong_match_threshold else full), exact


# ---------------------------------------------------------------------------
# V11.1: the title scorer on trial (AUD-R100)
#
# A port of the frozen V11.1 of the 2026-11-01 title-scoring design: v11.py, on variants.py's
# v10 and patterns.py, in docs/superpowers/specs/2026-11-01-title-scoring-v11-frozen (local to
# Lance's machine; it wins over any comment here). Lance's later decisions grade it blind, so it
# must give exactly the frozen scores: tests/services/test_title_scoring_v11.py checks every
# decided pair. Change nothing here without Lance. The functions keep the frozen names (_v6 to
# _v10, words, same, ...) so the two can be read side by side; rule ids (N1, S1-S11, G1-G3) are
# the design's. Each title's forms are cached by title, as _scoring_titles is (SPEC E6).
# ---------------------------------------------------------------------------

# A title scorer: (raw broadcast log title, library file title) -> score, 0-100.
TitleScore = Callable[[str, str], float]

_V11_CACHE_SIZE = 65536
# Under today's 80 strong-match threshold: a capped song goes to review, never auto-matches.
_REVIEW_CAP = 79.0
_PART_CAP = 60.0

_ARTICLES = ("the", "a", "an")
_TRUNCATION = re.compile(r"\.\.|…")
_SINGLE_LETTERS = re.compile(r"\b(?:[a-z0-9] )+[a-z0-9]\b")
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an) ")
_LEADING_DOTS = re.compile(r"^\s*(?:\.{2,}|…)")
_BRACKET_TO_END = re.compile(r"[\(\[][^\)\]]*(?:[\)\]]|$)")
_LOG_BRACKET = re.compile(r"[\(\[]([^\)\]]*)(?:[\)\]]|$)")
_CLOSED_BRACKET = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_SPELLED_OUT = re.compile(r"\b(?:[A-Za-z][-.]){3,}")
_CREDIT_TO_END = re.compile(r"\b[fw]/\S*.*$")
_FILE_MEDLEY_SLASH = re.compile(r"\s+/\s+|/")
_NUMBER_SIGN = re.compile(r"#\s*(\d)")
_WITHOUT = re.compile(r"\b[Ww]/[Oo]ut\b")
_PART_TOKENS = re.compile(r"\b(?:part|parts|pt)\s+(?:\d+|[ivx]+|one|two|three|four)\b")

# S8: radio shorthand, and the letters that sound alike.
_SHORTHAND = {
    "4": "for",
    "u": "you",
    "2": "to",
    "r": "are",
    "n": "and",
    "b4": "before",
    "tha": "the",
    "da": "the",
    "thru": "through",
    "tonite": "tonight",
    "xmas": "christmas",
}
_SOUND_ALIKES = (
    ("ph", "f"),
    ("gh", ""),
    ("ck", "k"),
    ("ch", "k"),
    ("qu", "kw"),
    ("wr", "r"),
    ("kn", "n"),
    ("x", "ks"),
    ("z", "s"),
    ("y", "i"),
)
_TO_WORDS = ("2", "to", "too")
# patterns.py's word expansions, as frozen ("w out" and "no" never change a word).
_EXPAND = {
    "nyc": ["new", "york", "city"],
    "mf": ["motherfucker"],
    "w out": ["without"],
    "wout": ["without"],
    "2": ["to"],
    "no": ["no"],
}


def v11_file_title(f: LibraryFile) -> str:
    """The file title V11.1 scores: the tag's track_title, else the stored normalized_title."""
    return f.audio.track_title or f.audio.normalized_title or ""


def v11_title_score(log_title: str, file_title: str) -> float:
    """V11.1's score of a raw broadcast log title against a library file's title, 0-100.

    v11.py: V10, then S9 (one title starts the other: 85), S10 (every word agrees: 90), a log
    title equal to one segment of a medley file (79) and S11 (the log's bracket is the file's
    title: 85), then the caps again, so those rules can lift a score past G1's 79 but never
    past a cap. A title that normalizes to nothing scores 0.
    """
    tt = _mask_bracket_slashes(file_title)
    score = _v10(log_title, tt)
    lw, fw = _words(log_title), _words(tt)
    if _one_starts_other(lw, fw):
        score = max(score, 85.0)
    if (
        len(lw) >= 2
        and len(lw) == len(fw)
        and all(_same(a, b) for a, b in zip(lw, fw, strict=True))
    ):
        score = max(score, 90.0)
    if _medley_segment_equal(log_title, tt):
        score = max(score, 79.0)
    if _bracket_is_title(log_title, tt):
        score = max(score, 85.0)
    return float(_caps(score, log_title, tt))


# --- variants.py: V6 to V10 ------------------------------------------------------------------


def _v10(raw: str, tt: str) -> float:
    """V9 with the caps G2, G3 and S6 applied last."""
    return _caps(_v9(raw, tt), raw, tt)


def _caps(score: float, raw: str, tt: str) -> float:
    """G2: a medley file never auto-matches unless the log has the slash too. G3: a title
    spelled out on one side only. S6: different part numbers on both sides."""
    if " / " in tt and not _log_has_slash(raw):
        score = min(score, _REVIEW_CAP)
    if _spelled_out(raw) != _spelled_out(tt):
        score = min(score, _REVIEW_CAP)
    return _part_cap(score, raw, tt)


def _part_cap(score: float, raw: str, tt: str) -> float:
    pb, pf = _part_number(raw), _part_number(tt)
    if pb is not None and pf is not None and pb != pf:
        return min(score, _PART_CAP)
    return score


def _v9(raw: str, tt: str) -> float:
    """S7: titles equal once spaces and punctuation go score 100; else V8."""
    if _joined_equal(raw, tt):
        return 100.0
    return _v8(raw, tt)


def _v8(raw: str, tt: str) -> float:
    """S6b: the score drops to the mean of itself and the word score, when that is lower."""
    score = _v7(raw, tt)
    words = _word_score(raw, tt)
    return score if words >= 100 else min(score, (score + words) / 2)


def _v7(raw: str, tt: str) -> float:
    """G1 on scores of 80+: the words must agree, or the score is capped at 79."""
    score = _v6(raw, tt)
    if score < 80:
        return score
    agree = _words_agree(raw, tt)
    if " / " in tt and not agree:
        # matched one half of a medley file: worth showing, not worth auto-matching
        return min(score, _REVIEW_CAP)
    if _spelled_out(raw) != _spelled_out(tt):
        return min(score, _REVIEW_CAP)
    if not agree:
        log_side = raw.split("/") if "/" in raw else [raw]
        if not any(_words_agree(half, tt) for half in log_side):
            return min(score, _REVIEW_CAP)
    return score


def _v6(raw: str, tt: str) -> float:
    """S1-S6: the ratio score against the file title without its dash version suffix (S4),
    with medley segments capped at 85 and a log medley "A/B" also scored by halves (S5); then
    different part numbers cap it at 60 (S6)."""
    base_tt = extract_dash_version(tt)[0] or tt
    score = _variant(raw, base_tt, medley=False)
    if " / " in base_tt:
        # Without a " / " the medley forms are the whole title's, and so is the score.
        score = max(score, min(_variant(raw, base_tt, medley=True), 85.0))
    low = raw.lower()
    if "/" in raw and " / " not in raw and "f/" not in low and "w/" not in low:
        for half in raw.split("/"):
            if len(half.split()) >= 2:
                score = max(score, _variant(half, base_tt, medley=False))
    return _part_cap(score, raw, tt)


def _variant(raw: str, tt: str, *, medley: bool) -> float:
    """variants.py's variant() at V5's settings: the best full-form ratio, or a bracket-stripped
    one when that reaches 80, as today's _candidate_scores does; then S3, a cut-off log title
    whose 2+ kept words start a file form in order scores 96 (the last may be a prefix)."""
    full, forms, kept = _log_ratio_forms(raw)
    file_full, file_forms = _file_ratio_forms(tt, medley)
    base = max((_order_ratio(b, file_full) for b in full), default=0.0)
    stripped = max((_order_ratio(b, f) for b in forms for f in file_forms), default=0.0)
    score = stripped if stripped >= 80 else base
    if not kept:
        return score
    n = len(kept)
    for form in file_forms:
        words = form.split()
        if len(words) < n:
            continue
        if tuple(words[: n - 1]) == kept[:-1] and words[n - 1].startswith(kept[-1]):
            score = max(score, 96.0)
        else:
            score = max(score, ratio(" ".join(kept), " ".join(words[:n])))
    return score


def _order_ratio(a: str, b: str) -> float:
    """S2: word order counts, as the mean of token_sort_ratio and ratio."""
    return (token_sort_ratio(a, b) + ratio(a, b)) / 2


@lru_cache(maxsize=_V11_CACHE_SIZE)
def _log_ratio_forms(raw: str) -> tuple[frozenset[str], frozenset[str], tuple[str, ...]]:
    """(full forms, full and bracket-stripped forms, kept words) of a log title for _variant.

    The full forms are the raw title with its guest credits stripped; the kept words are a
    cut-off title's, only when there are 2 or more (S3).
    """
    raws = _credit_forms(raw)
    full = frozenset(_v11_form(r) for r in raws) - {""}
    core = frozenset(_v11_form(strip_bracketed_groups(r)) for r in raws) - {""}
    prefix = _truncated_prefix(raw)
    kept = tuple(_v11_form(prefix).split()) if prefix is not None else ()
    return full, full | core, kept if len(kept) >= 2 else ()


@lru_cache(maxsize=_V11_CACHE_SIZE)
def _file_ratio_forms(tt: str, medley: bool) -> tuple[str, frozenset[str]]:
    """(full form, all forms) of a file title for _variant: with and without its bracketed
    groups and, for a medley, each " / " segment the same way."""
    forms = {tt, strip_bracketed_groups(tt)}
    if medley and " / " in tt:
        for seg in tt.split(" / "):
            forms |= {seg, strip_bracketed_groups(seg)}
    return _v11_form(tt), frozenset(_v11_form(f) for f in forms if f.strip()) - {""}


def _truncated_prefix(raw: str) -> str | None:
    """What a cut-off log title kept: the text before its first ".."/"…" or before an unclosed
    bracket. None when the title is whole."""
    m = _TRUNCATION.search(raw)
    if m:
        return raw[: m.start()]
    if raw.count("(") > raw.count(")"):
        return raw[: raw.rfind("(")]
    return None


@lru_cache(maxsize=_V11_CACHE_SIZE)
def _v11_form(raw: str) -> str:
    """A title's V11.1 form: normalized as today's forms are, after N1 (a dot or ellipsis
    becomes a space; single letters joined by dots run together, "R.O.C.K." -> "rock"), and
    without a leading the/a/an (S1)."""
    s = raw.replace("…", "...").replace(".", " ")
    s = normalize_title_for_scoring(normalize_title(s))
    s = _SINGLE_LETTERS.sub(lambda m: m.group().replace(" ", ""), s)
    return _LEADING_ARTICLE.sub("", s)


@lru_cache(maxsize=_V11_CACHE_SIZE)
def _content_words(raw: str) -> tuple[str, ...]:
    """A title's V11.1 form as words, without the/a/an."""
    return tuple(w for w in _v11_form(raw).split() if w not in _ARTICLES)


@lru_cache(maxsize=_V11_CACHE_SIZE)
def _credit_forms(raw: str) -> tuple[str, ...]:
    return tuple(_credit_stripped_forms(raw))


# Word agreement: G1, S6b and S7 compare these forms.


@lru_cache(maxsize=_V11_CACHE_SIZE)
def _log_forms(raw: str) -> tuple[tuple[tuple[str, ...], bool], ...]:
    """(words, cut off) forms of a log title: credits stripped, a leading "..." ignored, its
    brackets kept as words, stripped, or dropped with a cut-off inside one."""
    out: list[tuple[tuple[str, ...], bool]] = []
    for r in _credit_forms(raw):
        r = _LEADING_DOTS.sub("", r)
        for form in {r, strip_bracketed_groups(r), _BRACKET_TO_END.sub("", r)}:
            prefix = _truncated_prefix(form)
            out.append((_content_words(prefix if prefix is not None else form), prefix is not None))
    return tuple(dict.fromkeys(out))


@lru_cache(maxsize=_V11_CACHE_SIZE)
def _file_forms(tt: str) -> tuple[tuple[str, ...], ...]:
    """A file title's word forms: with and without its dash version suffix and its brackets."""
    base = extract_dash_version(tt)[0] or tt
    forms = {tt, base, strip_bracketed_groups(tt), strip_bracketed_groups(base)}
    return tuple({words for f in forms if (words := _content_words(f))})


def _words_agree(log_raw: str, file_raw: str) -> bool:
    """G1: every content word pairs, in order, with the same word, a typo or a sound-alike; or
    the words run together. A cut-off log title only needs its kept words."""
    return any(
        _agree(lw, fw, cut) for lw, cut in _log_forms(log_raw) for fw in _file_forms(file_raw)
    )


def _agree(lw: tuple[str, ...], fw: tuple[str, ...], cut: bool) -> bool:
    if not lw:
        return False
    if ratio("".join(lw), "".join(fw)) >= 92:
        return True
    if cut:
        fw = fw[: len(lw)]
    if len(lw) != len(fw):
        return False
    last = len(lw) - 1
    return all(
        _same_word(a, b, cut and i == last) for i, (a, b) in enumerate(zip(lw, fw, strict=True))
    )


def _word_score(log_raw: str, file_raw: str) -> float:
    """S6b: the share of words that agree in order (their longest common run), 0-100, over the
    longer title or a cut-off log title's kept words; the best of the forms counts."""
    best = 0.0
    for lw, cut in _log_forms(log_raw):
        for fw in _file_forms(file_raw):
            if not lw:
                continue
            if ratio("".join(lw), "".join(fw)) >= 92:
                return 100.0
            if cut:
                fw = fw[: len(lw)]
            last = len(lw) - 1
            m = [[0] * (len(fw) + 1) for _ in range(len(lw) + 1)]
            for i, a in enumerate(lw):
                for j, b in enumerate(fw):
                    m[i + 1][j + 1] = (
                        m[i][j] + 1
                        if _same_word(a, b, cut and i == last)
                        else max(m[i][j + 1], m[i + 1][j])
                    )
            best = max(best, 100.0 * m[-1][-1] / max(len(lw), len(fw)))
    return best


def _joined_equal(raw: str, tt: str) -> bool:
    """S7: a whole log form and a file form are equal once their spaces go."""
    return any(
        "".join(lw) == "".join(fw)
        for lw, cut in _log_forms(raw)
        if not cut
        for fw in _file_forms(tt)
    )


@lru_cache(maxsize=4 * _V11_CACHE_SIZE)
def _same_word(a: str, b: str, cut_last: bool) -> bool:
    """Equal, a typo (same first letter, ratio 75+), the cut-off last word's prefix, or (S8) a
    sound-alike sharing half its letters, or radio shorthand."""
    if a == b or (a[0] == b[0] and ratio(a, b) >= 75) or (cut_last and b.startswith(a)):
        return True
    ka, kb = _sound_key(a), _sound_key(b)
    # a sound-alike must also share at least half its letters ("Mein" is not "Moana")
    return (
        (len(ka) >= 2 and ka == kb and ratio(a, b) >= 55)
        or _SHORTHAND.get(a) == b
        or _SHORTHAND.get(b) == a
    )


@lru_cache(maxsize=_V11_CACHE_SIZE)
def _sound_key(w: str) -> str:
    """A rough spoken form: 'phantom'/'fantom' -> fntm, 'night'/'nite' -> nt, 'enough'/'enuff'
    -> enf."""
    w = _SHORTHAND.get(w, w)
    w = re.sub(r"ough$", "uf", w)
    for a, b in _SOUND_ALIKES:
        w = w.replace(a, b)
    w = re.sub(r"c(?=[eiy])", "s", w).replace("c", "k")
    w = re.sub(r"(.)\1+", r"\1", w)
    return w[:1] + re.sub(r"[aeiou]", "", w[1:])


# The caps' tests, each on one title.


def _log_has_slash(raw: str) -> bool:
    return "/" in _CREDIT_TO_END.sub("", raw.lower())


@lru_cache(maxsize=_V11_CACHE_SIZE)
def _spelled_out(raw: str) -> bool:
    """G3: spelled out letter by letter ("Y.M.C.A."), judged without the guest credit."""
    return bool(_SPELLED_OUT.search(_credit_forms(raw)[0]))


@lru_cache(maxsize=_V11_CACHE_SIZE)
def _part_number(raw: str) -> int | None:
    return title_part_number(normalize_title(raw))


@lru_cache(maxsize=_V11_CACHE_SIZE)
def _mask_bracket_slashes(title: str) -> str:
    """A slash inside brackets is not a medley: 'Uninvited (Live / Unplugged)'."""
    return _CLOSED_BRACKET.sub(lambda m: m.group().replace("/", ","), title)


# --- patterns.py: the word rules S9-S11 ------------------------------------------------------


@lru_cache(maxsize=_V11_CACHE_SIZE)
def _words(raw: str) -> tuple[str, ...]:
    """Content words with the credit, a dash suffix, brackets and "part N" off; '#' -> 'no',
    '+'/'&' -> 'and'. A plain digit ('Driver 8', '4 U') stays."""
    r = _credit_forms(raw)[0]
    r = extract_dash_version(r)[0] or r
    r = _NUMBER_SIGN.sub(r"no \1", r).replace("+", " and ").replace("&", " and ")
    r = _WITHOUT.sub("without", r)
    r = _PART_TOKENS.sub(" ", r.lower())
    out: list[str] = []
    for w in _content_words(strip_bracketed_groups(r)):
        out += _EXPAND.get(w, [w])
    return tuple(out)


def _same(a: str, b: str) -> bool:
    """_same_word with sound-alikes, "2"/"to"/"too", and a censored word ('f***ing') whose
    letters and length fit."""
    if _same_word(a, b, False) or (a in _TO_WORDS and b in _TO_WORDS):
        return True
    for c, w in ((a, b), (b, a)):
        if "*" in c and len(c) == len(w):
            return bool(re.fullmatch(c.replace("*", "."), w))
    return False


def _one_starts_other(lw: tuple[str, ...], fw: tuple[str, ...]) -> bool:
    """S9: the shorter title (2+ words, one of 4+ letters: not 'I Am') is, word for word, the
    start of the longer one."""
    s, longer = (lw, fw) if len(lw) <= len(fw) else (fw, lw)
    return (
        len(s) >= 2
        and len(longer) > len(s)
        and all(_same(a, b) for a, b in zip(s, longer, strict=False))
        and any(len(w) >= 4 for w in s)
    )


def _medley_segment_equal(raw: str, tt: str) -> bool:
    """S5: the log title is word for word one ' / ' segment of the file title (or the
    reverse)."""
    if "/" not in tt:
        return False
    segs = [_words(s) for s in _FILE_MEDLEY_SLASH.split(tt)]
    if "/" in raw and "f/" not in raw.lower():
        lsegs = [_words(s) for s in raw.split("/")]
    else:
        lsegs = [_words(raw)]
    return any(
        len(a) == len(b) and a and all(_same(x, y) for x, y in zip(a, b, strict=True))
        for a in lsegs
        for b in segs
    )


def _bracket_is_title(raw: str, tt: str) -> bool:
    """S11: a bracketed group in the log title (2+ words; 3+ kept words when cut off) is the
    file's title, possibly cut off: 'Wake Up (Make A Move)' -> 'Make a Move'."""
    fw = _words(tt)
    return any(_agree_words(bw, fw, cut) for bw, cut in _log_brackets(raw))


@lru_cache(maxsize=_V11_CACHE_SIZE)
def _log_brackets(raw: str) -> tuple[tuple[tuple[str, ...], bool], ...]:
    """(words, cut off) of each bracketed group in a log title with enough words for S11."""
    out: list[tuple[tuple[str, ...], bool]] = []
    for m in _LOG_BRACKET.finditer(raw):
        inner = m.group(1)
        cut = bool(_TRUNCATION.search(inner)) or not m.group(0).endswith((")", "]"))
        bw = _words(_TRUNCATION.split(inner)[0])
        if len(bw) >= (3 if cut else 2):
            out.append((bw, cut))
    return tuple(out)


def _agree_words(a: tuple[str, ...], b: tuple[str, ...], cut: bool) -> bool:
    if cut:
        b = b[: len(a)]
        return (
            len(a) >= 2
            and len(b) == len(a)
            and all(_same(x, y) for x, y in zip(a[:-1], b[:-1], strict=True))
            and (_same(a[-1], b[-1]) or b[-1].startswith(a[-1]))
        )
    return bool(a) and len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))

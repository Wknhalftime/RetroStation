"""The words and the title the cache fill searches by (phase 4.1 PR 2; SPEC E5, S3; the
coordinator's readings for PR 2's search words, applying design C2, C8, C67 and C69).

``name_words(original_name, *, truncated)`` gives the words a name + title search requires
(every one of them, AND). They come from the log's original name, as C2 reads it (cleaning drops
"feat. X"):

- the name is split on whitespace and on the joiners, ``&``, ``and``, ``with``, ``/`` and ``,``,
  plus C69's featuring words (``FEATURING_WORDS``); a joiner is never sent, and never splits the
  name into artists (that is 4.2's reading of the hits);
- punctuation is trimmed from each word's ends;
- ``the``, ``a`` and ``an`` are dropped, as title scoring drops them;
- an initial is dropped: one letter once dots and apostrophes go ("J.", "N'"), or a dotted run
  of single letters ("R.E.M.", "B.B.");
- a name the stage reads as cut off (``truncated``) drops its last word, which may be partial.

A name left with no words is searched by its title alone, and the entry says ``title_only``
(E5, PR 1's method). The words keep the log's case: the search ignores case and the cache key
lower-cases them.

``search_title(original_title)`` gives the title phrase: a title ending in "..." or "…" drops
the mark and the word it cuts (C67's prefix match), and the phrase that is left still matches
the whole title's start.
"""

from __future__ import annotations

import pytest

from backend.services.cache_fill_words import name_words, search_title


@pytest.mark.parametrize(
    ("original", "words"),
    [
        # The coordinator's examples (readings 1 and 2).
        ("R.E.M.", ()),
        ("THE THE", ()),
        ("J.J. CALE", ("CALE",)),
        ("B.B. KING", ("KING",)),
        ("GUNS N' ROSES", ("GUNS", "ROSES")),
        ("THE EAGLES", ("EAGLES",)),
        ("HALL AND OATES", ("HALL", "OATES")),
        ("AC/DC", ("AC", "DC")),
        ("EARTH, WIND & FIRE", ("EARTH", "WIND", "FIRE")),
        # Joiners and featuring words, in any case, are never sent.
        ("QUEEN & DAVID BOWIE", ("QUEEN", "DAVID", "BOWIE")),
        ("CROSBY, STILLS & NASH", ("CROSBY", "STILLS", "NASH")),
        ("HOOTIE AND THE BLOWFISH", ("HOOTIE", "BLOWFISH")),
        ("ANDY GIBB WITH OLIVIA NEWTON-JOHN", ("ANDY", "GIBB", "OLIVIA", "NEWTON-JOHN")),
        ("SANTANA FT. ROB THOMAS", ("SANTANA", "ROB", "THOMAS")),
        ("SANTANA FT ROB THOMAS", ("SANTANA", "ROB", "THOMAS")),
        ("Santana feat. Rob Thomas", ("Santana", "Rob", "Thomas")),
        ("SANTANA FEAT ROB THOMAS", ("SANTANA", "ROB", "THOMAS")),
        ("Santana Featuring Rob Thomas", ("Santana", "Rob", "Thomas")),
        ("Brooks and Dunn", ("Brooks", "Dunn")),
        ("Simon With Garfunkel", ("Simon", "Garfunkel")),
        # The symbol joiners split inside a word too; the word joiners only stand alone.
        ("SIMON&GARFUNKEL", ("SIMON", "GARFUNKEL")),
        ("BRANDY", ("BRANDY",)),
        ("WITHIN TEMPTATION", ("WITHIN", "TEMPTATION")),
        # Initials go; words that merely start with a letter and a dot or an apostrophe stay.
        ("R. KELLY", ("KELLY",)),
        ("SINEAD O'CONNOR", ("SINEAD", "O'CONNOR")),
        ("B-52'S", ("B-52'S",)),
        ("A-HA", ("A-HA",)),
        # Numbers are words; so are two-letter words.
        ("38 SPECIAL", ("38", "SPECIAL")),
        ("UB40", ("UB40",)),
        ("ZZ TOP", ("ZZ", "TOP")),
        # Punctuation is trimmed from a word's ends; extra spaces don't make words.
        ('"WEIRD AL" YANKOVIC', ("WEIRD", "AL", "YANKOVIC")),
        ("  TOM   PETTY  ", ("TOM", "PETTY")),
        # Nothing to search by.
        ("", ()),
        ("&", ()),
        ("A", ()),
    ],
)
def test_name_words(original: str, words: tuple[str, ...]) -> None:
    assert name_words(original, truncated=False) == words


@pytest.mark.parametrize(
    ("original", "words"),
    [
        # A cut-off name drops its last word: it may be partial.
        ("FLAKY BAND THAT ALMOST RESOLV", ("FLAKY", "BAND", "THAT", "ALMOST")),
        # Then the other rules apply to what is left.
        ("THE ALLMAN BR", ("ALLMAN",)),
        ("EARTH, WIND & FIRE WITH THE EM", ("EARTH", "WIND", "FIRE")),
        # The cut word is the last token as logged, before joiners, articles and initials go:
        # "T" is a cut word here, so GARFUNKEL stays.
        ("SIMON & GARFUNKEL WITH T", ("SIMON", "GARFUNKEL")),
        # A joiner inside a word splits it first, so only its cut end goes.
        ("AC/D", ("AC",)),
        # A one-word cut-off name is left with nothing: a title-only search.
        ("SUPERCALIFRAGILISTICEXPIALIDOC", ()),
    ],
)
def test_a_cut_off_name_drops_its_last_word(original: str, words: tuple[str, ...]) -> None:
    assert name_words(original, truncated=True) == words


def test_a_name_that_is_not_cut_off_keeps_its_last_word() -> None:
    assert name_words("FLAKY BAND THAT ALMOST RESOLV", truncated=False) == (
        "FLAKY",
        "BAND",
        "THAT",
        "ALMOST",
        "RESOLV",
    )


@pytest.mark.parametrize(
    ("original", "title"),
    [
        ("UNDER PRESSURE", "UNDER PRESSURE"),
        ("Mama Don't Get Dressed Up for Nothing", "Mama Don't Get Dressed Up for Nothing"),
        # A cut title drops the mark and the word it cuts (C67).
        ("I'LL BE HOME FOR CHRIST...", "I'LL BE HOME FOR"),
        ("I'LL BE HOME FOR CHRIST…", "I'LL BE HOME FOR"),
        # A mark after a space cuts no word: only the mark goes.
        ("I'LL BE HOME FOR ...", "I'LL BE HOME FOR"),
        # Surrounding spaces don't reach the phrase.
        ("  UNDER PRESSURE  ", "UNDER PRESSURE"),
        # A title that is only a cut word leaves nothing to search: the fill skips the pair.
        ("SOMETHI...", ""),
        ("...", ""),
    ],
)
def test_search_title(original: str, title: str) -> None:
    assert search_title(original) == title

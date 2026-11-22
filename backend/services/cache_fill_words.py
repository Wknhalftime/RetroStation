"""The words and the title text the cache fill searches by (MusicBrainz cache fill, phase 4.1 PR 2;
SPEC E5, S3; the coordinator's readings for PR 2's search words, applying design C2, C8, C67 and
C69; AUD-R091).

A name + title search requires every word it is given (PR 1's
``search_recordings_by_name_title``), so a word MusicBrainz spells another way makes it miss for
good: the miss is cached (E2). The words are therefore the log's own spelling, less what
MusicBrainz writes differently:

- the joiners (``&``, ``and``, ``with``, ``/``, ``,`` and C69's featuring words), which never
  split a name into artists here (that is 4.2's reading of the hits);
- ``the``, ``a`` and ``an``, as title scoring drops them;
- initials, which the index may hold as "REM" or "R.E.M.".

A name the stage reads as cut off drops its last token first, before any of that: it may be
partial. A name left with no words is searched by its title alone (E5).
"""

from __future__ import annotations

import re
import string

from backend.domain.performances import FEATURING_WORDS

# Joiners that are words of their own; "BRANDY" and "WITHIN" hold one and split on none.
_WORD_JOINERS = frozenset({"and", "with"}) | FEATURING_WORDS
# Joiners that split wherever they stand: "SIMON&GARFUNKEL", "AC/DC", "CROSBY, STILLS".
_SYMBOL_JOINERS = re.compile(r"[&/,]")
_STOP_WORDS = frozenset({"the", "a", "an"})
_TRIMMED = string.punctuation + "‘’“”…"
# A dotted run of single letters: "R.E.M", "B.B", "J.J" (its last dot already trimmed).
_DOTTED_INITIALS = re.compile(r"[^\W\d_](?:\.[^\W\d_])+")
_CUT_MARKS = ("...", "…")


def _tokens(name: str) -> list[str]:
    """The name split on whitespace and on the symbol joiners, empty pieces dropped."""
    return [piece for word in name.split() for piece in _SYMBOL_JOINERS.split(word) if piece]


def _kept(token: str) -> str | None:
    """The word a token sends, or None when it sends none (a joiner, an article, an initial)."""
    word = token.strip(_TRIMMED)
    if not word or token.lower() in _WORD_JOINERS or word.lower() in _WORD_JOINERS:
        return None
    if word.lower() in _STOP_WORDS:
        return None
    letters = word.replace(".", "").replace("'", "").replace("’", "")
    if (len(letters) == 1 and letters.isalpha()) or _DOTTED_INITIALS.fullmatch(word):
        return None
    return word


def name_words(original_name: str, *, truncated: bool) -> tuple[str, ...]:
    """The words a name + title search requires for this log name, in the log's case.

    ``truncated`` is the stage's own reading (``is_likely_truncated``): the last token is then
    dropped before the other rules, since it may be partial.
    """
    tokens = _tokens(original_name)
    if truncated:
        tokens = tokens[:-1]
    return tuple(word for word in map(_kept, tokens) if word is not None)


def has_joiner(name: str) -> bool:
    """Whether a name joins several performers (C68, C18): a symbol joiner anywhere, or a word
    joiner standing alone."""
    if _SYMBOL_JOINERS.search(name):
        return True
    return any(word.lower() in _WORD_JOINERS for word in name.split())


def search_title(original_title: str) -> str:
    """The title phrase to search: a title ending in "..." or "…" drops the mark and the word it
    cuts (C67), so the phrase still matches the whole title's start. A mark after a space cuts no
    word. A title that is only a cut word gives "", and the fill skips the pair."""
    title = original_title.strip()
    if not title.endswith(_CUT_MARKS):
        return title
    body = title.rstrip(".…")
    if not body or body[-1].isspace():
        return body.strip()
    head = body.rsplit(maxsplit=1)
    return head[0].strip() if len(head) == 2 else ""

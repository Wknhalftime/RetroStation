"""The log-side version reader (versions phase 2b, SPEC S10; design C63, D4).

A log line's version text (its trailing bracket groups, lowercased and joined: the generated
``track_identities.version_text``, C59) read as a :class:`VersionType`, ``UNKNOWN`` for title
words. It drives the Library's version pages only (which texts read as versions, the
new-version marker, suggestions, the master flag), never grouping, matching or air play.

Its own reader, beside the file side's ``classify_version_descriptor``: log abbreviations and
file tags change for different reasons (D4), so the two tables are kept apart even where they
agree today. This one is the file side's table as phase 2b PR 1 left it, plus the log's
abbreviations and venues.
"""

from __future__ import annotations

import re

from backend.domain.enums import VersionType

# Ordered: the first rule that matches wins. Multi-word phrases before single keywords, word
# boundaries against substrings ("alive" is not "live", "silver" not "lv"). A rule mapping to
# UNKNOWN marks title words a later rule would misread.
_LOG_VERSION_RULES: list[tuple[re.Pattern[str], VersionType]] = [
    # Multi-word phrases
    (re.compile(r"rough mix"), VersionType.DEMO),
    (re.compile(r"radio edit|radio version|single version"), VersionType.RADIO_EDIT),
    (re.compile(r"club mix|extended mix"), VersionType.REMIX),
    (re.compile(r"long version|full version"), VersionType.EXTENDED),
    (re.compile(r"original recording|original version"), VersionType.ORIGINAL),
    (re.compile(r"backing track"), VersionType.INSTRUMENTAL),
    (re.compile(r"in concert"), VersionType.LIVE),
    (re.compile(r"cover version"), VersionType.COVER),
    (re.compile(r"a\s+cappella"), VersionType.A_CAPPELLA),
    (
        re.compile(r"deluxe edition|special edition|limited edition|anniversary edition"),
        VersionType.EDITION,
    ),
    (re.compile(r"alt version|alternate version|alternative version"), VersionType.ALTERNATE),
    # Single keywords
    (re.compile(r"\blive\b|\bconcert\b"), VersionType.LIVE),
    (re.compile(r"\bacoustic\b|unplugged"), VersionType.ACOUSTIC),
    # C63, the log's own: "Lv" (and "Lv. Ac.", a live acoustic take, as "Live Acoustic"
    # reads), S&M2, BBC sessions, the Telethon and Ed Sullivan read live; "Unplgd", "Acous"
    # and "Stripped" acoustic. Below the live and acoustic keywords and the phrases, so those
    # still win.
    (re.compile(r"\blv\b|\bs&m(?: ?2)?\b|\bbbc\b|\btelethon\b|\bed sullivan\b"), VersionType.LIVE),
    (re.compile(r"\bunplgd\b|\bacous\b|\bstripped\b"), VersionType.ACOUSTIC),
    # C63: "Original Mix" is the original; just ahead of the bare "mix" rule.
    (re.compile(r"\boriginal mix\b"), VersionType.ORIGINAL),
    (re.compile(r"\b(?:remix|remixed)\b|\bdub\b|\bmix\b"), VersionType.REMIX),
    (re.compile(r"remaster"), VersionType.REMASTER),
    (re.compile(r"edition"), VersionType.EDITION),
    (re.compile(r"\bedit\b"), VersionType.RADIO_EDIT),
    (re.compile(r"\bdemo\b"), VersionType.DEMO),
    (re.compile(r"extended"), VersionType.EXTENDED),
    (re.compile(r"instrumental"), VersionType.INSTRUMENTAL),
    (re.compile(r"original"), VersionType.ORIGINAL),
    (re.compile(r"explicit|lyrical|\blanguage\b|\blyrics\b"), VersionType.EXPLICIT),
    (re.compile(r"\bclean\b"), VersionType.CLEAN),
    (re.compile(r"\bcover\b"), VersionType.COVER),
    # C63: "(I'm Special)" is a title, not the edition the bare "special" below would read.
    (re.compile(r"\bi['\u2019]m special\b"), VersionType.UNKNOWN),
    (re.compile(r"deluxe|bonus|anniversary|\bspecial\b|\blimited\b"), VersionType.EDITION),
    (re.compile(r"\balt\b|alternate|alternative"), VersionType.ALTERNATE),
    (re.compile(r"\bmono\b|\bstereo\b"), VersionType.FORMAT),
    (re.compile(r"^\d{4}\s*[/,\-\u2013]\s*\S"), VersionType.LIVE),
    # Catch-alls, after every specific rule. C63: "Amended" is clean, an album, LP or studio
    # version the original; just ahead of the "version" catch-all.
    (re.compile(r"\bamended\b"), VersionType.CLEAN),
    (re.compile(r"\b(?:album|lp|studio) version\b"), VersionType.ORIGINAL),
    (re.compile(r"\bversion\b"), VersionType.REMIX),
    (re.compile(r"\bbootleg\b"), VersionType.REMIX),
    (re.compile(r"\bvideo\b"), VersionType.REMIX),
    (re.compile(r"\bstudio\b"), VersionType.REMIX),
    (re.compile(r"\bintro\b"), VersionType.REMIX),
    (re.compile(r"\bcut\b"), VersionType.REMIX),
    (re.compile(r"\bfade\b"), VersionType.REMIX),
    (re.compile(r"\bversi[oó]n\b"), VersionType.REMIX),
    (re.compile(r"\ben\s+(?:vivo|directo)\b"), VersionType.REMIX),
    (re.compile(r"\bac[uú]stic[oa]\b"), VersionType.REMIX),
]


def read_log_version(version_text: str) -> VersionType:
    """The version a log line's version text names, or ``UNKNOWN`` for title words."""
    text = version_text.lower().strip()
    if not text:
        return VersionType.UNKNOWN
    for pattern, version_type in _LOG_VERSION_RULES:
        if pattern.search(text):
            return version_type
    return VersionType.UNKNOWN

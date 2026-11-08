"""The log-side version reader (versions phase 2b, SPEC S10; design C63, D4).

``read_log_version(version_text)`` reads a log line's version text (its trailing bracket groups,
lowercased and joined, C59) as a :class:`VersionType`, ``UNKNOWN`` for title words. It is its
own reader, beside the file side's ``classify_version_descriptor``, because log abbreviations and
file tags change for different reasons (D4):

- it learns the log's abbreviations and venues: "Lv", "Lv. Ac.", "S&M2" (and a bare "S&M",
  Metallica's 1999 live album), "BBC", "Telethon" and "Ed Sullivan" read as live, "Unplgd",
  "Acous" and "Stripped" as acoustic. "Lv. Ac." is a live acoustic take and reads live, as
  "Live Acoustic" does on either side;
- it reads "Album Version", "LP Version", "Studio Version" and "Original Mix" as the original,
  "Amended" as clean and "(I'm Special)" as title words, as PR 1 taught the file side, and each
  sits where it does there, so every reading above it still wins;
- every other descriptor reads as the file side reads it today.

It drives the Library's version pages only (S5's "reads as a version", S8's marker, S11's
suggestions, C66's master flag), never grouping, matching or air play.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.domain.enums import VersionType
from backend.domain.log_version_reader import read_log_version

ROOT = Path(__file__).resolve().parents[2]

LIVE = VersionType.LIVE
ACOUSTIC = VersionType.ACOUSTIC
ORIGINAL = VersionType.ORIGINAL
CLEAN = VersionType.CLEAN
UNKNOWN = VersionType.UNKNOWN


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("lv", LIVE),
        ("lv.", LIVE),
        ("lv. ac.", LIVE),
        ("lv ac", LIVE),
        ("live acoustic", LIVE),
        ("s&m2", LIVE),
        ("s&m 2", LIVE),
        ("s&m", LIVE),
        ("live s&m", LIVE),
        ("bbc", LIVE),
        ("bbc session", LIVE),
        ("telethon", LIVE),
        ("ed sullivan", LIVE),
        ("ed sullivan show", LIVE),
        ("unplgd", ACOUSTIC),
        ("acous", ACOUSTIC),
        ("acous.", ACOUSTIC),
        ("stripped", ACOUSTIC),
        ("stripped down", ACOUSTIC),
    ],
)
def test_the_log_abbreviations_and_venues(text: str, expected: VersionType) -> None:
    assert read_log_version(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("album version", ORIGINAL),
        ("lp version", ORIGINAL),
        ("studio version", ORIGINAL),
        ("original mix", ORIGINAL),
        ("amended", CLEAN),
        ("amended version", CLEAN),
        ("i'm special", UNKNOWN),
        ("i’m special", UNKNOWN),
    ],
)
def test_the_file_sides_misreads_are_not_repeated(text: str, expected: VersionType) -> None:
    assert read_log_version(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("live album version", LIVE),
        ("lv album version", LIVE),
        ("explicit album version", VersionType.EXPLICIT),
        ("live original mix", LIVE),
        ("original club mix", VersionType.REMIX),
        ("original rough mix", VersionType.DEMO),
        ("amended radio edit", VersionType.RADIO_EDIT),
        ("live amended", LIVE),
        ("radio edit lv", VersionType.RADIO_EDIT),
    ],
)
def test_a_reading_above_still_wins(text: str, expected: VersionType) -> None:
    """Each fix and each abbreviation sits below the multi-word phrases and the live and
    acoustic keywords, so a text that names one of those reads as before."""
    assert read_log_version(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("live", LIVE),
        ("live '09", LIVE),
        ("part 1 live", LIVE),
        ("live explicit", LIVE),
        ("unplugged", ACOUSTIC),
        ("acoustic", ACOUSTIC),
        ("'08 mix", VersionType.REMIX),
        ("remix", VersionType.REMIX),
        ("radio edit", VersionType.RADIO_EDIT),
        ("single version", VersionType.RADIO_EDIT),
        ("remastered 2011", VersionType.REMASTER),
        ("demo", VersionType.DEMO),
        ("extended", VersionType.EXTENDED),
        ("instrumental", VersionType.INSTRUMENTAL),
        ("explicit", VersionType.EXPLICIT),
        ("clean", CLEAN),
        ("mono", VersionType.FORMAT),
        ("version 2", VersionType.REMIX),
    ],
)
def test_every_other_descriptor_reads_as_the_file_side_reads_it(
    text: str, expected: VersionType
) -> None:
    assert read_log_version(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "war is over",
        "part 1",
        "silver lining",
        "acoustics",
        "stayin' alive",
        "",
        "   ",
    ],
)
def test_title_words_read_as_unknown(text: str) -> None:
    """Word boundaries keep "lv" out of "silver" and "live" out of "alive"."""
    assert read_log_version(text) == UNKNOWN


@pytest.mark.parametrize(
    ("text", "expected"),
    [("LV", LIVE), (" Unplgd ", ACOUSTIC), ("Album Version", ORIGINAL), ("BBC Session", LIVE)],
)
def test_case_and_surrounding_spaces_do_not_matter(text: str, expected: VersionType) -> None:
    """The column is already lowercased; the reader lowercases and trims anyway, as the file
    side's reader does."""
    assert read_log_version(text) == expected


# The file side, the matcher and air play: none of them reads the log reader (S10).
_NOT_READERS = [
    "backend/services/normalization.py",
    "backend/services/grouping_service.py",
    "backend/services/library_enrichment_service.py",
    "backend/services/untagged_recording_service.py",
    "backend/services/title_scoring.py",
    "scripts/retitle_works.py",
    "backend/services/*match*.py",
    "backend/services/streaming/**/*.py",
    "backend/playout/**/*.py",
    "backend/db/repositories/play_file_resolution.py",
    "backend/db/repositories/playable_schedule.py",
    "backend/db/repositories/stream_cue_work.py",
    "backend/tasks/*match*.py",
    "backend/services/master_*.py",
    "scripts/airplay_snapshot.py",
]


def test_grouping_matching_and_air_play_never_read_it() -> None:
    found = {pattern: sorted(ROOT.glob(pattern)) for pattern in _NOT_READERS}
    readers = [
        path.relative_to(ROOT).as_posix()
        for paths in found.values()
        for path in paths
        if "log_version_reader" in path.read_text(encoding="utf-8")
    ]

    assert [pattern for pattern, paths in found.items() if not paths] == []  # none went stale
    assert readers == []

"""The priority days of cue pre-computation.

Spec: D62 ("today's and tomorrow's playlists for every station, over every year from its
first to its last logged play"); Tune-in step 1 ("An impossible date (29 Feb in a non-leap
year) ... NoBroadcastError"); D29/D39 (the walk continues into the next calendar day,
Dec 31 -> Jan 1).
"""

from __future__ import annotations

from datetime import date

from backend.domain.tune_in import tune_in_days


def test_the_days_are_today_and_tomorrow_in_every_logged_year() -> None:
    """D62: today's and tomorrow's playlists in every logged year."""
    assert tune_in_days(date(2026, 3, 14), range(1995, 1998)) == [
        date(1995, 3, 14),
        date(1995, 3, 15),
        date(1996, 3, 14),
        date(1996, 3, 15),
        date(1997, 3, 14),
        date(1997, 3, 15),
    ]


def test_tomorrow_rolls_into_the_next_year() -> None:
    """D29/D39: a listener tuned in on 31 Dec keeps playing into 1 Jan of the next year."""
    assert tune_in_days(date(2026, 12, 31), range(1995, 1997)) == [
        date(1995, 12, 31),
        date(1996, 1, 1),
        date(1996, 12, 31),
        date(1997, 1, 1),
    ]


def test_29_february_has_no_broadcast_in_a_year_without_it() -> None:
    """Tune-in step 1: 29 Feb in a non-leap year is no broadcast, so it has no days."""
    assert tune_in_days(date(2028, 2, 29), range(1995, 1997)) == [
        date(1996, 2, 29),
        date(1996, 3, 1),
    ]


def test_the_day_before_29_february_walks_into_it_in_a_leap_year() -> None:
    """The walk's next calendar day: 28 Feb -> 29 Feb in a leap year, -> 1 Mar otherwise."""
    assert tune_in_days(date(2027, 2, 28), range(1995, 1997)) == [
        date(1995, 2, 28),
        date(1995, 3, 1),
        date(1996, 2, 28),
        date(1996, 2, 29),
    ]


def test_no_logged_years_means_no_days() -> None:
    """An empty log has no station-years, so nothing is prioritised."""
    assert tune_in_days(date(2026, 3, 14), range(0)) == []

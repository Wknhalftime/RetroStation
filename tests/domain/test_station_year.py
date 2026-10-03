"""A station-year and the days its log covers (spec D70: "Coverage on the station-year list is
days logged only ("362 of 365 days")"; value objects validate in ``__post_init__``; the
/listen year range 1..9999)."""

from __future__ import annotations

import pytest

from backend.domain.streaming import InvalidStreamValueError, StationYear


@pytest.mark.parametrize(("year", "days"), [(1995, 365), (1996, 366), (2000, 366), (1900, 365)])
def test_days_in_year_counts_leap_years(year: int, days: int) -> None:
    assert StationYear(call_letters="KIOA", year=year, days_logged=1).days_in_year == days


@pytest.mark.parametrize(
    ("year", "days_logged"), [(1995, 0), (1995, 366), (1995, -1)], ids=["0", "366", "-1"]
)
def test_days_logged_is_within_the_year(year: int, days_logged: int) -> None:
    with pytest.raises(InvalidStreamValueError, match=r"StationYear\.days_logged"):
        StationYear(call_letters="KIOA", year=year, days_logged=days_logged)


@pytest.mark.parametrize(
    ("call_letters", "year", "field_name"),
    [("KIOA", 0, "year"), ("KIOA", 10_000, "year"), ("", 1995, "call_letters")],
    ids=["year 0", "year 10000", "no call letters"],
)
def test_a_station_year_needs_a_calendar_year_and_call_letters(
    call_letters: str, year: int, field_name: str
) -> None:
    with pytest.raises(InvalidStreamValueError, match=rf"StationYear\.{field_name}"):
        StationYear(call_letters=call_letters, year=year, days_logged=1)

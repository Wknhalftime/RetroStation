"""The station-year list (spec D70: coverage is days logged only, no per-day file check and no
whole-year percentage; the F2 contract: ordered by call letters ignoring case, then year;
a year with no logged day is absent; plan R2: a ``broadcast_days`` row is a logged day)."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from backend.domain.broadcast import BroadcastStation
from backend.domain.streaming import StationYear
from backend.services.streaming.station_years import StationYearRepos, list_station_years
from tests.fakes.broadcast_days import FakeBroadcastDayRepository
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository


@pytest.fixture
def repos() -> StationYearRepos:
    return StationYearRepos(
        stations=FakeBroadcastStationRepository(), days=FakeBroadcastDayRepository()
    )


def station(repos: StationYearRepos, call_letters: str, *days: date) -> None:
    """A station whose log covers ``days``."""
    stored = repos.stations.create(BroadcastStation(id=uuid4(), call_letters=call_letters))
    for day in days:
        repos.days.get_or_create(stored.id, day)


def test_each_station_year_counts_its_logged_days(repos: StationYearRepos) -> None:
    station(
        repos,
        "KIOA",
        date(1995, 3, 14),
        date(1995, 3, 15),
        date(1995, 12, 31),
        date(1996, 1, 1),
    )
    assert list_station_years(repos) == [
        StationYear(call_letters="KIOA", year=1995, days_logged=3),
        StationYear(call_letters="KIOA", year=1996, days_logged=1),
    ]


def test_a_year_without_a_logged_day_is_not_listed(repos: StationYearRepos) -> None:
    # D70: KIOA has no 2013 on dev
    station(repos, "KIOA", date(2012, 9, 30), date(2014, 1, 2))
    assert [row.year for row in list_station_years(repos)] == [2012, 2014]


def test_a_station_without_logged_days_is_not_listed(repos: StationYearRepos) -> None:
    station(repos, "KNRK")
    station(repos, "KSTZ", date(2001, 9, 1))
    assert list_station_years(repos) == [StationYear(call_letters="KSTZ", year=2001, days_logged=1)]


def test_rows_are_ordered_by_call_letters_ignoring_case_then_year(
    repos: StationYearRepos,
) -> None:
    # Plain string order would put "KIOA" before "Kazr" ("I" < "a"); casefold puts it after
    station(repos, "KIOA", date(1996, 5, 1), date(1995, 5, 1))
    station(repos, "Kazr", date(2002, 1, 1), date(2001, 1, 1))
    assert [(row.call_letters, row.year) for row in list_station_years(repos)] == [
        ("Kazr", 2001),
        ("Kazr", 2002),
        ("KIOA", 1995),
        ("KIOA", 1996),
    ]

"""The station-year list: every station-year with at least one logged day (D70).

Coverage is days logged only ("362 of 365 days"): no per-day file check and no whole-year
percentage. Call letters match in any case and twins are blocked (D72), so ordering by
``call_letters.casefold()`` then year is total. The list works while ``stream_service`` is
``None``: it reads only the stations and broadcast-days repositories.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from backend.domain.streaming import StationYear
from backend.repositories.broadcast_days import BroadcastDayRepository
from backend.repositories.broadcast_stations import BroadcastStationRepository


@dataclass(frozen=True)
class StationYearRepos:
    """The repositories the station-year list reads."""

    stations: BroadcastStationRepository
    days: BroadcastDayRepository


def list_station_years(repos: StationYearRepos) -> list[StationYear]:
    """Every station-year with at least one logged day, ordered by
    ``call_letters.casefold()``, then year (D70). A year with no logged day is absent."""
    rows = [
        StationYear(call_letters=station.call_letters, year=year, days_logged=days_logged)
        for station in repos.stations.list_all()
        for year, days_logged in Counter(
            day.year for day in repos.days.get_dates_for_station(station.id)
        ).items()
    ]
    return sorted(rows, key=lambda row: (row.call_letters.casefold(), row.year))

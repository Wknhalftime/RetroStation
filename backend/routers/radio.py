"""Public radio routes (spec D71): outside ``/api/v1``, no ``X-Airwave-Token``, reachable
from the LAN. The station-year list (D70) is served here; it works while streaming is off
(D34 does not gate it), so this router depends on no stream service.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from backend.dependencies import get_station_year_repos
from backend.domain.system import StorageUnavailableError
from backend.services.streaming.station_years import StationYearRepos, list_station_years

router = APIRouter(prefix="/radio")

StationYearRepositories = Annotated[StationYearRepos, Depends(get_station_year_repos)]


class StationYearOut(BaseModel):
    """One row of the station-year list, over HTTP (the F2 contract)."""

    call_letters: str
    year: int
    days_logged: int
    days_in_year: int


@router.get("/station-years", response_model=list[StationYearOut])
def get_station_years(repos: StationYearRepositories) -> list[StationYearOut]:
    """Every station-year with at least one logged day (D70); public, no token (D71). A lost
    database connection answers 503."""
    try:
        rows = list_station_years(repos)
    except StorageUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable") from exc
    return [
        StationYearOut(
            call_letters=row.call_letters,
            year=row.year,
            days_logged=row.days_logged,
            days_in_year=row.days_in_year,
        )
        for row in rows
    ]

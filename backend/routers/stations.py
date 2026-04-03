from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

import psycopg
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.dependencies import get_current_token, get_db_connection

router = APIRouter()

DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
Token = Annotated[str, Depends(get_current_token)]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class StationCreate(BaseModel):
    call_letters: str
    name: str | None = None
    city: str | None = None
    format_name: str | None = None


class StationUpdate(BaseModel):
    call_letters: str | None = None
    name: str | None = None
    city: str | None = None
    format_name: str | None = None


class StationResponse(BaseModel):
    id: UUID
    call_letters: str
    name: str | None = None
    city: str | None = None
    format_name: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class StationSummary(StationResponse):
    playlist_count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_response(row: dict[str, Any]) -> StationResponse:
    return StationResponse(
        id=row["id"],
        call_letters=row["call_letters"],
        name=row.get("name"),
        city=row.get("city"),
        format_name=row.get("format_name"),
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=list[StationSummary])
async def list_stations(conn: DbConn, _token: Token) -> list[StationSummary]:
    """List all stations with aggregate playlist counts."""
    cur = await conn.execute(
        """
        SELECT
            s.id,
            s.call_letters,
            s.name,
            s.city,
            s.format_name,
            s.created_at,
            COUNT(p.id) AS playlist_count
        FROM stations s
        LEFT JOIN playlists p ON p.station_id = s.id
        GROUP BY s.id, s.call_letters, s.name, s.city, s.format_name, s.created_at
        ORDER BY s.call_letters
        """
    )
    rows = await cur.fetchall()
    return [
        StationSummary(
            id=row["id"],
            call_letters=row["call_letters"],
            name=row.get("name"),
            city=row.get("city"),
            format_name=row.get("format_name"),
            created_at=row["created_at"],
            playlist_count=row["playlist_count"],
        )
        for row in rows
    ]


@router.post("", response_model=StationResponse, status_code=status.HTTP_201_CREATED)
async def create_station(
    body: StationCreate, conn: DbConn, _token: Token
) -> StationResponse:
    """Create a new station."""
    station_id = uuid4()
    try:
        await conn.execute(
            """
            INSERT INTO stations (id, call_letters, name, city, format_name)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (station_id, body.call_letters, body.name, body.city, body.format_name),
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Station with call_letters '{body.call_letters}' already exists",
        ) from exc
    cur = await conn.execute(
        "SELECT * FROM stations WHERE id = %s", (station_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("Expected row after INSERT")
    return _row_to_response(row)


@router.get("/{station_id}", response_model=StationResponse)
async def get_station(station_id: UUID, conn: DbConn, _token: Token) -> StationResponse:
    """Retrieve a single station by ID."""
    cur = await conn.execute(
        "SELECT * FROM stations WHERE id = %s", (station_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Station {station_id} not found",
        )
    return _row_to_response(row)


@router.put("/{station_id}", response_model=StationResponse)
async def update_station(
    station_id: UUID, body: StationUpdate, conn: DbConn, _token: Token
) -> StationResponse:
    """Partially update a station (only provided fields are changed)."""
    cur = await conn.execute(
        "SELECT * FROM stations WHERE id = %s", (station_id,)
    )
    existing = await cur.fetchone()
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Station {station_id} not found",
        )

    # Merge: only overwrite fields explicitly set in the request body
    updated = body.model_dump(exclude_unset=True)
    new_call_letters = updated.get("call_letters", existing["call_letters"])
    new_name = updated.get("name", existing.get("name"))
    new_city = updated.get("city", existing.get("city"))
    new_format_name = updated.get("format_name", existing.get("format_name"))

    await conn.execute(
        """
        UPDATE stations
        SET call_letters = %s, name = %s, city = %s, format_name = %s
        WHERE id = %s
        """,
        (new_call_letters, new_name, new_city, new_format_name, station_id),
    )
    cur = await conn.execute(
        "SELECT * FROM stations WHERE id = %s", (station_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("Expected row after INSERT")
    return _row_to_response(row)


@router.delete("/{station_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_station(
    station_id: UUID, conn: DbConn, _token: Token
) -> None:
    """Delete a station by ID."""
    cur = await conn.execute(
        "SELECT id FROM stations WHERE id = %s", (station_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Station {station_id} not found",
        )
    await conn.execute("DELETE FROM stations WHERE id = %s", (station_id,))

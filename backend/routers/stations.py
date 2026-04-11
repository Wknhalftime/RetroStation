from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel

from backend.config import get_settings
from backend.dependencies import get_current_token, get_db_connection
from backend.services.m3u_generator_service import generate_m3u
from backend.services.repository_factory import RepositoryFactory

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


class StationEventItem(BaseModel):
    """A single log event with joined identity/artist info and source playlist name."""

    id: UUID
    played_at: datetime
    artist_name: str
    title: str
    match_status: str
    match_tier: str | None
    playlist_name: str

    model_config = {"from_attributes": True}


class StationPaginatedEvents(BaseModel):
    """Paginated wrapper for a station's events on a date."""

    items: list[StationEventItem]
    total: int


class StationExportM3uBody(BaseModel):
    """Body for the station M3U export endpoint."""

    date: date


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


async def _require_station(conn: AsyncConnection[Any], station_id: UUID) -> dict[str, Any]:
    """Fetch a station row or raise 404."""
    cur = await conn.execute("SELECT * FROM stations WHERE id = %s", (station_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Station {station_id} not found",
        )
    return row


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


# ---------------------------------------------------------------------------
# Calendar / event endpoints
# ---------------------------------------------------------------------------


@router.get("/{station_id}/broadcast-days", response_model=list[str])
async def get_station_broadcast_days(
    station_id: UUID, conn: DbConn, _token: Token,
) -> list[str]:
    """Return ISO date strings for all broadcast days for this station."""
    await _require_station(conn, station_id)
    cur = await conn.execute(
        """
        SELECT DISTINCT broadcast_date
        FROM broadcast_days
        WHERE station_id = %s
        ORDER BY broadcast_date
        """,
        (station_id,),
    )
    rows = await cur.fetchall()
    return [row["broadcast_date"].isoformat() for row in rows]


@router.get("/{station_id}/events", response_model=StationPaginatedEvents)
async def get_station_events_by_date(
    station_id: UUID,
    conn: DbConn,
    _token: Token,
    date: date = Query(...),  # noqa: B008
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> StationPaginatedEvents:
    """Return paginated events for a station on a given date across all playlists."""
    await _require_station(conn, station_id)

    # Total count
    count_cur = await conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM play_events le
        JOIN playlists p ON p.id = le.playlist_id
        WHERE p.station_id = %s AND le.played_at::date = %s
        """,
        (station_id, date),
    )
    count_row = await count_cur.fetchone()
    total = count_row["total"] if count_row else 0

    # Paginated items
    items_cur = await conn.execute(
        """
        SELECT
            le.id,
            le.played_at,
            la.original_name AS artist_name,
            li.original_title AS title,
            li.match_status,
            li.match_tier,
            p.name AS playlist_name
        FROM play_events le
        JOIN track_identities li ON li.id = le.identity_id
        JOIN broadcast_artists la ON la.id = li.broadcast_artist_id
        JOIN playlists p ON p.id = le.playlist_id
        WHERE p.station_id = %s AND le.played_at::date = %s
        ORDER BY le.played_at
        LIMIT %s OFFSET %s
        """,
        (station_id, date, limit, offset),
    )
    rows = await items_cur.fetchall()
    items = [
        StationEventItem(
            id=row["id"],
            played_at=row["played_at"],
            artist_name=row["artist_name"],
            title=row["title"],
            match_status=row["match_status"],
            match_tier=row["match_tier"],
            playlist_name=row["playlist_name"],
        )
        for row in rows
    ]
    return StationPaginatedEvents(items=items, total=total)


def _generate_station_m3u_sync(
    station_id_str: str,
    date_str: str,
    database_url: str,
    station_format: str | None,
) -> str:
    """Run M3U generation for a station+date on a sync connection."""
    from datetime import date as date_type
    sid = UUID(station_id_str)
    d = date_type.fromisoformat(date_str)
    with psycopg.connect(database_url, row_factory=dict_row) as sync_conn:
        repos = RepositoryFactory(sync_conn)
        events = repos.play_events.get_by_station_date(sid, d)
        return generate_m3u(
            events=events,
            identity_repo=repos.track_identities,
            match_repo=repos.matches,
            file_repo=repos.library_files,
            recording_repo=repos.recordings,
            master_repo=repos.song_masters,
            override_repo=repos.format_overrides,
            settings_repo=repos.settings,
            station_format=station_format,
        )


@router.post("/{station_id}/export-m3u")
async def export_station_m3u(
    station_id: UUID,
    conn: DbConn,
    _token: Token,
    body: StationExportM3uBody,
) -> Response:
    """Generate and return an M3U file for a station on a given date."""
    station_row = await _require_station(conn, station_id)
    call_letters = station_row["call_letters"]

    database_url = get_settings().database_url
    station_format = station_row.get("format_name")

    m3u_content = await asyncio.to_thread(
        _generate_station_m3u_sync,
        str(station_id),
        body.date.isoformat(),
        database_url,
        station_format,
    )

    filename = f"{call_letters}-{body.date.isoformat()}.m3u"
    return Response(
        content=m3u_content,
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

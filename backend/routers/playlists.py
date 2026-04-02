from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.dependencies import get_current_token, get_db_connection

router = APIRouter()

DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
Token = Annotated[str, Depends(get_current_token)]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PlaylistSummary(BaseModel):
    """Playlist list item with aggregate event count."""

    id: UUID
    name: str
    station_id: UUID | None
    content_hash: str
    ingested_at: datetime
    event_count: int

    model_config = {"from_attributes": True}


class PlaylistDetail(BaseModel):
    """Full playlist record."""

    id: UUID
    name: str
    station_id: UUID | None
    content_hash: str
    ingested_at: datetime

    model_config = {"from_attributes": True}


class EventItem(BaseModel):
    """A single log event with joined identity/artist info."""

    id: UUID
    played_at: datetime
    artist_name: str
    title: str
    match_status: str
    match_tier: str | None

    model_config = {"from_attributes": True}


class PaginatedEvents(BaseModel):
    """Paginated wrapper for a playlist's events."""

    items: list[EventItem]
    total: int


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=list[PlaylistSummary])
async def list_playlists(
    conn: DbConn,
    _token: Token,
    station_id: UUID = Query(...),  # noqa: B008
) -> list[PlaylistSummary]:
    """List playlists for a station, ordered by ingested_at DESC.

    Args:
        conn: Async database connection.
        _token: Bearer token (auth check only).
        station_id: Required station UUID filter.

    Returns:
        List of playlist summaries including event counts.
    """
    cur = await conn.execute(
        """
        SELECT
            p.id,
            p.name,
            p.station_id,
            p.content_hash,
            p.ingested_at,
            COUNT(le.id) AS event_count
        FROM playlists p
        LEFT JOIN log_events le ON le.playlist_id = p.id
        WHERE p.station_id = %s
        GROUP BY p.id, p.name, p.station_id, p.content_hash, p.ingested_at
        ORDER BY p.ingested_at DESC
        """,
        (station_id,),
    )
    rows = await cur.fetchall()
    return [
        PlaylistSummary(
            id=row["id"],
            name=row["name"],
            station_id=row["station_id"],
            content_hash=row["content_hash"],
            ingested_at=row["ingested_at"],
            event_count=row["event_count"],
        )
        for row in rows
    ]


@router.get("/{playlist_id}", response_model=PlaylistDetail)
async def get_playlist(
    playlist_id: UUID,
    conn: DbConn,
    _token: Token,
) -> PlaylistDetail:
    """Retrieve a single playlist by ID.

    Args:
        playlist_id: UUID of the playlist to fetch.
        conn: Async database connection.
        _token: Bearer token (auth check only).

    Returns:
        Full playlist detail.

    Raises:
        HTTPException: 404 if the playlist does not exist.
    """
    cur = await conn.execute(
        "SELECT id, name, station_id, content_hash, ingested_at FROM playlists WHERE id = %s",
        (playlist_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Playlist {playlist_id} not found",
        )
    return PlaylistDetail(
        id=row["id"],
        name=row["name"],
        station_id=row["station_id"],
        content_hash=row["content_hash"],
        ingested_at=row["ingested_at"],
    )


@router.get("/{playlist_id}/events", response_model=PaginatedEvents)
async def get_playlist_events(
    playlist_id: UUID,
    conn: DbConn,
    _token: Token,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> PaginatedEvents:
    """Return paginated events for a playlist with artist/identity info.

    Args:
        playlist_id: UUID of the playlist.
        conn: Async database connection.
        _token: Bearer token (auth check only).
        limit: Maximum number of items to return (1-500, default 50).
        offset: Number of items to skip (default 0).

    Returns:
        Paginated events with total count.

    Raises:
        HTTPException: 404 if the playlist does not exist.
    """
    # Verify playlist exists first
    exists_cur = await conn.execute(
        "SELECT id FROM playlists WHERE id = %s",
        (playlist_id,),
    )
    if await exists_cur.fetchone() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Playlist {playlist_id} not found",
        )

    # Total count
    count_cur = await conn.execute(
        "SELECT COUNT(*) AS total FROM log_events WHERE playlist_id = %s",
        (playlist_id,),
    )
    count_row = await count_cur.fetchone()
    total = count_row["total"] if count_row else 0

    # Paginated items with joins
    items_cur = await conn.execute(
        """
        SELECT
            le.id,
            le.played_at,
            la.original_name AS artist_name,
            li.original_title AS title,
            li.match_status,
            li.match_tier
        FROM log_events le
        JOIN log_identities li ON li.id = le.identity_id
        JOIN log_artists la ON la.id = li.artist_id
        WHERE le.playlist_id = %s
        ORDER BY le.played_at
        LIMIT %s OFFSET %s
        """,
        (playlist_id, limit, offset),
    )
    rows = await items_cur.fetchall()
    items = [
        EventItem(
            id=row["id"],
            played_at=row["played_at"],
            artist_name=row["artist_name"],
            title=row["title"],
            match_status=row["match_status"],
            match_tier=row["match_tier"],
        )
        for row in rows
    ]
    return PaginatedEvents(items=items, total=total)


@router.get("/{playlist_id}/broadcast-days", response_model=list[str])
async def get_broadcast_days(
    playlist_id: UUID,
    conn: DbConn,
    _token: Token,
) -> list[str]:
    """Return ISO date strings for broadcast days linked to a playlist's station.

    Args:
        playlist_id: UUID of the playlist.
        conn: Async database connection.
        _token: Bearer token (auth check only).

    Returns:
        Sorted list of ISO date strings (YYYY-MM-DD). Empty list if no station.

    Raises:
        HTTPException: 404 if the playlist does not exist.
    """
    pl_cur = await conn.execute(
        "SELECT station_id FROM playlists WHERE id = %s",
        (playlist_id,),
    )
    pl_row = await pl_cur.fetchone()
    if pl_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Playlist {playlist_id} not found",
        )

    station_id: UUID | None = pl_row["station_id"]
    if station_id is None:
        return []

    days_cur = await conn.execute(
        """
        SELECT broadcast_date
        FROM broadcast_days
        WHERE station_id = %s
        ORDER BY broadcast_date
        """,
        (station_id,),
    )
    rows = await days_cur.fetchall()
    return [row["broadcast_date"].isoformat() for row in rows]

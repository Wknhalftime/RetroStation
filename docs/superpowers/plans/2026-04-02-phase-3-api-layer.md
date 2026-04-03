# Phase 3 — API Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build all REST API endpoints and WebSocket the React frontend needs, so Phase 4 can code against live data.

**Architecture:** Thin FastAPI routers using the async connection pool (`Depends(get_db_connection)`). Direct async SQL for reads. `asyncio.to_thread` with sync `RepositoryFactory` for complex write operations. Pydantic v2 response schemas defined inline per router. TDD with integration tests against the real test database via `TestClient`.

**Tech Stack:** FastAPI, psycopg 3 (async), Pydantic v2, pytest, structlog

---

## File Map

### New Files

| File | Responsibility |
|------|---------------|
| `backend/routers/stations.py` | Stations CRUD + aggregate stats |
| `backend/routers/playlists.py` | Playlist list/detail/events |
| `backend/routers/matching.py` | Review queue, manual resolution, re-run trigger |
| `backend/routers/settings.py` | User settings get/put |
| `backend/routers/tasks.py` | Active background tasks |
| `backend/websocket.py` | Progress broadcast over WebSocket |
| `backend/services/m3u_generator_service.py` | Priority chain resolution + Navidrome path mapping |
| `backend/db/repositories/settings.py` | PgSettingsRepository |
| `backend/db/repositories/format_overrides.py` | PgFormatOverrideRepository |
| `tests/routers/__init__.py` | Package init |
| `tests/routers/conftest.py` | TestClient + DB fixtures for router tests |
| `tests/routers/test_stations.py` | Station endpoint tests |
| `tests/routers/test_playlists.py` | Playlist endpoint tests |
| `tests/routers/test_library.py` | Library endpoint tests |
| `tests/routers/test_matching.py` | Matching endpoint tests |
| `tests/routers/test_settings.py` | Settings endpoint tests |
| `tests/routers/test_tasks.py` | Tasks endpoint tests |
| `tests/test_websocket.py` | WebSocket tests |
| `tests/services/test_m3u_generator.py` | M3U generator unit tests |
| `tests/integration/test_pg_settings_repo.py` | PgSettingsRepository integration tests |
| `tests/integration/test_pg_format_overrides_repo.py` | PgFormatOverrideRepository integration tests |

### Modified Files

| File | Change |
|------|--------|
| `backend/dependencies.py` | Add auto-commit after yield in `get_db_connection` |
| `backend/routers/v1.py` | Register all new routers |
| `backend/routers/library.py` | Add GET endpoints (status, artists, works) |
| `backend/main.py` | Register WebSocket route |
| `backend/services/repository_factory.py` | Add `settings` and `format_overrides` attributes |

---

## API Shape Reference

All endpoints require `X-Airwave-Token` header (except `/health` and `/ws`).

### Stations — `/api/v1/stations`

| Method | Path | Request | Response | Status |
|--------|------|---------|----------|--------|
| GET | `/stations` | — | `list[StationSummary]` | 200 |
| POST | `/stations` | `StationCreate` body | `StationResponse` | 201 |
| GET | `/stations/{id}` | — | `StationDetail` | 200 |
| PUT | `/stations/{id}` | `StationUpdate` body | `StationResponse` | 200 |
| DELETE | `/stations/{id}` | — | — | 204 |

### Playlists — `/api/v1/playlists`

| Method | Path | Request | Response | Status |
|--------|------|---------|----------|--------|
| GET | `/playlists?station_id=` | query param | `list[PlaylistSummary]` | 200 |
| GET | `/playlists/{id}` | — | `PlaylistDetail` | 200 |
| GET | `/playlists/{id}/events?limit=50&offset=0` | query params | `PaginatedEvents` | 200 |
| GET | `/playlists/{id}/broadcast-days` | — | `list[str]` (ISO dates) | 200 |

### Library — `/api/v1/library`

| Method | Path | Request | Response | Status |
|--------|------|---------|----------|--------|
| POST | `/library/scan` | `ScanRequest` body | accepted msg | 202 |
| GET | `/library/status` | — | `LibraryStatus` | 200 |
| GET | `/library/artists?limit=50&offset=0&search=` | query params | `PaginatedArtists` | 200 |
| GET | `/library/artists/{id}` | — | `ArtistDetail` | 200 |
| GET | `/library/works/{id}` | — | `WorkDetail` | 200 |
| PUT | `/library/works/{id}/master` | `SetMasterBody` | `SongMasterResponse` | 200 |
| DELETE | `/library/works/{id}/master` | — | — | 204 |
| POST | `/library/works/{id}/format-overrides` | `CreateOverrideBody` | `FormatOverrideResponse` | 201 |
| DELETE | `/library/works/{id}/format-overrides/{oid}` | — | — | 204 |

### Matching — `/api/v1/matching`

| Method | Path | Request | Response | Status |
|--------|------|---------|----------|--------|
| GET | `/matching/queue?limit=50&offset=0` | query params | `MatchingQueue` | 200 |
| POST | `/matching/artists/{id}/resolve` | `ArtistResolution` body | `ResolveResult` | 200 |
| POST | `/matching/identities/{id}/resolve` | `IdentityResolution` body | `ResolveResult` | 200 |
| POST | `/matching/run` | — | accepted msg | 202 |

### Settings — `/api/v1/settings`

| Method | Path | Request | Response | Status |
|--------|------|---------|----------|--------|
| GET | `/settings` | — | `dict[str, str]` | 200 |
| PUT | `/settings/{key}` | `SettingValue` body | `SettingEntry` | 200 |

### Tasks — `/api/v1/tasks`

| Method | Path | Request | Response | Status |
|--------|------|---------|----------|--------|
| GET | `/tasks/active` | — | `list[TaskInfo]` | 200 |

### M3U Export — on playlists router

| Method | Path | Request | Response | Status |
|--------|------|---------|----------|--------|
| POST | `/playlists/{id}/export-m3u` | `ExportM3uBody` (optional) | `text/x-mpegurl` file | 200 |

### WebSocket — `/ws`

| Path | Auth | Behavior |
|------|------|----------|
| `/ws?token=` | query param token | Polls `progress_tracking` every 500ms, broadcasts JSON |

---

## Task 1: Router Test Infrastructure + Stations CRUD

**Files:**
- Create: `tests/routers/__init__.py`, `tests/routers/conftest.py`, `tests/routers/test_stations.py`
- Create: `backend/routers/stations.py`
- Modify: `backend/dependencies.py`, `backend/routers/v1.py`

### Step 1 — Write router test infrastructure

- [ ] **Step 1a: Create test conftest**

```python
# tests/routers/__init__.py
```

```python
# tests/routers/conftest.py
from __future__ import annotations

import os
from collections.abc import Generator

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row


@pytest.fixture(scope="session")
def _router_client(_migrated_db_url: str) -> Generator[TestClient]:
    """Session-scoped TestClient. Pool created once, reused across tests."""
    os.environ["DATABASE_URL"] = _migrated_db_url

    from backend.config import get_settings

    get_settings.cache_clear()

    from backend.dependencies import get_current_token
    from backend.main import app

    async def _skip_auth() -> str:
        return "test-token"

    app.dependency_overrides[get_current_token] = _skip_auth

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def client(_router_client: TestClient, migrated_db: str) -> TestClient:
    """Per-test client: tables are truncated before each test."""
    return _router_client


@pytest.fixture
def db_conn(migrated_db: str) -> Generator[psycopg.Connection[dict]]:
    """Sync connection for inserting test data."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        yield conn
```

- [ ] **Step 1b: Update `get_db_connection` to auto-commit**

```python
# backend/dependencies.py — replace get_db_connection
async def get_db_connection() -> AsyncGenerator[AsyncConnection[Any]]:
    pool = get_pool()
    async with pool.connection() as conn:
        yield conn
        await conn.commit()
```

- [ ] **Step 1c: Run existing tests to verify no breakage**

Run: `uv run pytest tests/ -x -q`
Expected: All 231 tests still pass.

### Step 2 — Write failing station tests

- [ ] **Step 2a: Write tests for list + create + get**

```python
# tests/routers/test_stations.py
from __future__ import annotations

from uuid import uuid4

import psycopg

from backend.db.repositories.stations import PgStationRepository
from backend.domain.models import Station


def _insert_station(
    conn: psycopg.Connection, call_letters: str, **kwargs
) -> Station:
    station = Station(id=uuid4(), call_letters=call_letters, **kwargs)
    result = PgStationRepository(conn).create(station)
    conn.commit()
    return result


class TestListStations:
    def test_empty(self, client):
        resp = client.get("/api/v1/stations")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_all(self, client, db_conn):
        _insert_station(db_conn, "KAZR-FM", name="Laser 103.3", city="Waukee")
        _insert_station(db_conn, "KIOA-FM", name="KIOA", city="Des Moines")
        resp = client.get("/api/v1/stations")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # Sorted by call_letters
        assert data[0]["call_letters"] == "KAZR-FM"
        assert data[1]["call_letters"] == "KIOA-FM"

    def test_includes_playlist_count(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        # Insert a playlist for this station
        from backend.db.repositories.playlists import PgPlaylistRepository
        from backend.domain.models import Playlist

        PgPlaylistRepository(db_conn).create(
            Playlist(
                id=uuid4(), name="test.csv", content_hash="abc123",
                station_id=station.id,
            )
        )
        db_conn.commit()
        resp = client.get("/api/v1/stations")
        data = resp.json()
        assert data[0]["playlist_count"] == 1


class TestCreateStation:
    def test_create(self, client):
        resp = client.post(
            "/api/v1/stations",
            json={
                "call_letters": "KAZR-FM",
                "name": "Laser 103.3",
                "city": "Waukee",
                "format_name": "CHR",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["call_letters"] == "KAZR-FM"
        assert data["name"] == "Laser 103.3"
        assert "id" in data

    def test_duplicate_call_letters_409(self, client, db_conn):
        _insert_station(db_conn, "KAZR-FM")
        resp = client.post(
            "/api/v1/stations", json={"call_letters": "KAZR-FM"}
        )
        assert resp.status_code == 409


class TestGetStation:
    def test_found(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM", name="Laser 103.3")
        resp = client.get(f"/api/v1/stations/{station.id}")
        assert resp.status_code == 200
        assert resp.json()["call_letters"] == "KAZR-FM"

    def test_not_found(self, client):
        resp = client.get(f"/api/v1/stations/{uuid4()}")
        assert resp.status_code == 404


class TestUpdateStation:
    def test_update(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM", name="Old Name")
        resp = client.put(
            f"/api/v1/stations/{station.id}",
            json={"name": "Laser 103.3"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Laser 103.3"
        # call_letters unchanged
        assert resp.json()["call_letters"] == "KAZR-FM"

    def test_not_found(self, client):
        resp = client.put(
            f"/api/v1/stations/{uuid4()}", json={"name": "x"}
        )
        assert resp.status_code == 404


class TestDeleteStation:
    def test_delete(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        resp = client.delete(f"/api/v1/stations/{station.id}")
        assert resp.status_code == 204
        # Verify gone
        resp = client.get(f"/api/v1/stations/{station.id}")
        assert resp.status_code == 404

    def test_not_found(self, client):
        resp = client.delete(f"/api/v1/stations/{uuid4()}")
        assert resp.status_code == 404
```

- [ ] **Step 2b: Run tests, confirm failures**

Run: `uv run pytest tests/routers/test_stations.py -v`
Expected: All tests FAIL (router not implemented yet).

### Step 3 — Implement stations router

- [ ] **Step 3a: Create `backend/routers/stations.py`**

```python
# backend/routers/stations.py
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.dependencies import get_current_token, get_db_connection

router = APIRouter()

DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
Token = Annotated[str, Depends(get_current_token)]


# ── Schemas ────────────────────────────────────────────────────────────

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
    name: str | None
    city: str | None
    format_name: str | None
    created_at: datetime


class StationSummary(StationResponse):
    playlist_count: int


# ── Endpoints ──────────────────────────────────────────────────────────

@router.get("")
async def list_stations(conn: DbConn, _token: Token) -> list[StationSummary]:
    cur = await conn.execute(
        """SELECT s.*,
                  COUNT(p.id) AS playlist_count
           FROM stations s
           LEFT JOIN playlists p ON p.station_id = s.id
           GROUP BY s.id
           ORDER BY s.call_letters"""
    )
    rows = await cur.fetchall()
    return [
        StationSummary(
            id=r["id"],
            call_letters=r["call_letters"],
            name=r.get("name"),
            city=r.get("city"),
            format_name=r.get("format_name"),
            created_at=r["created_at"],
            playlist_count=r["playlist_count"],
        )
        for r in rows
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_station(
    body: StationCreate, conn: DbConn, _token: Token,
) -> StationResponse:
    # Check for duplicate call_letters
    dup = await conn.execute(
        "SELECT id FROM stations WHERE call_letters = %s",
        (body.call_letters,),
    )
    if await dup.fetchone():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Station with call letters '{body.call_letters}' already exists",
        )

    station_id = uuid4()
    await conn.execute(
        """INSERT INTO stations (id, call_letters, name, city, format_name)
           VALUES (%s, %s, %s, %s, %s)""",
        (station_id, body.call_letters, body.name, body.city, body.format_name),
    )
    cur = await conn.execute(
        "SELECT * FROM stations WHERE id = %s", (station_id,)
    )
    row = await cur.fetchone()
    assert row is not None
    return StationResponse(
        id=row["id"],
        call_letters=row["call_letters"],
        name=row.get("name"),
        city=row.get("city"),
        format_name=row.get("format_name"),
        created_at=row["created_at"],
    )


@router.get("/{station_id}")
async def get_station(
    station_id: UUID, conn: DbConn, _token: Token,
) -> StationResponse:
    cur = await conn.execute(
        "SELECT * FROM stations WHERE id = %s", (station_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Station not found")
    return StationResponse(
        id=row["id"],
        call_letters=row["call_letters"],
        name=row.get("name"),
        city=row.get("city"),
        format_name=row.get("format_name"),
        created_at=row["created_at"],
    )


@router.put("/{station_id}")
async def update_station(
    station_id: UUID, body: StationUpdate, conn: DbConn, _token: Token,
) -> StationResponse:
    cur = await conn.execute(
        "SELECT * FROM stations WHERE id = %s", (station_id,)
    )
    existing = await cur.fetchone()
    if existing is None:
        raise HTTPException(status_code=404, detail="Station not found")

    new_letters = body.call_letters if body.call_letters is not None else existing["call_letters"]
    new_name = body.name if body.name is not None else existing.get("name")
    new_city = body.city if body.city is not None else existing.get("city")
    new_format = body.format_name if body.format_name is not None else existing.get("format_name")

    await conn.execute(
        """UPDATE stations
           SET call_letters = %s, name = %s, city = %s, format_name = %s
           WHERE id = %s""",
        (new_letters, new_name, new_city, new_format, station_id),
    )
    cur = await conn.execute(
        "SELECT * FROM stations WHERE id = %s", (station_id,)
    )
    row = await cur.fetchone()
    assert row is not None
    return StationResponse(
        id=row["id"],
        call_letters=row["call_letters"],
        name=row.get("name"),
        city=row.get("city"),
        format_name=row.get("format_name"),
        created_at=row["created_at"],
    )


@router.delete("/{station_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_station(
    station_id: UUID, conn: DbConn, _token: Token,
) -> None:
    cur = await conn.execute(
        "SELECT id FROM stations WHERE id = %s", (station_id,)
    )
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Station not found")
    await conn.execute("DELETE FROM stations WHERE id = %s", (station_id,))
```

- [ ] **Step 3b: Register in `v1.py`**

Replace the contents of `backend/routers/v1.py` with:

```python
from __future__ import annotations

from fastapi import APIRouter

from backend.routers import ingestion, library, stations

router = APIRouter(prefix="/api/v1")
router.include_router(stations.router, prefix="/stations", tags=["stations"])
router.include_router(ingestion.router, prefix="/ingestion", tags=["ingestion"])
router.include_router(library.router, prefix="/library", tags=["library"])
```

- [ ] **Step 3c: Run tests**

Run: `uv run pytest tests/routers/test_stations.py -v`
Expected: All tests PASS.

- [ ] **Step 3d: Run full test suite + type check**

Run: `uv run pytest tests/ -x -q && uv run mypy --strict backend/ && uv run ruff check backend/`
Expected: All pass, zero errors.

- [ ] **Step 3e: Commit**

```bash
git add tests/routers/ backend/routers/stations.py backend/routers/v1.py backend/dependencies.py
git commit -m "feat: stations CRUD router with aggregate stats"
```

---

## Task 2: Playlists — List, Detail, Events, Broadcast Days

**Files:**
- Create: `backend/routers/playlists.py`, `tests/routers/test_playlists.py`
- Modify: `backend/routers/v1.py`

### Step 1 — Write failing playlist tests

- [ ] **Step 1a: Create test file**

```python
# tests/routers/test_playlists.py
from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

import psycopg

from backend.db.repositories.broadcast_days import PgBroadcastDayRepository
from backend.db.repositories.log_artists import PgLogArtistRepository
from backend.db.repositories.log_events import PgLogEventRepository
from backend.db.repositories.log_identities import PgLogIdentityRepository
from backend.db.repositories.playlists import PgPlaylistRepository
from backend.db.repositories.stations import PgStationRepository
from backend.domain.models import (
    LogArtist,
    LogEvent,
    LogIdentity,
    Playlist,
    Station,
)


def _seed_station(conn: psycopg.Connection) -> Station:
    s = Station(id=uuid4(), call_letters="KAZR-FM")
    PgStationRepository(conn).create(s)
    conn.commit()
    return s


def _seed_playlist(conn: psycopg.Connection, station_id) -> Playlist:
    p = Playlist(
        id=uuid4(), name="test.csv", content_hash=uuid4().hex,
        station_id=station_id,
    )
    PgPlaylistRepository(conn).create(p)
    conn.commit()
    return p


def _seed_events(conn: psycopg.Connection, playlist_id, count: int = 3):
    """Seed log_artist → log_identity → log_event chain."""
    artist = LogArtist(
        id=uuid4(), original_name="Nirvana", normalized_name="nirvana",
    )
    PgLogArtistRepository(conn).upsert(artist)

    identity = LogIdentity(
        id=uuid4(), artist_id=artist.id,
        original_title="Smells Like Teen Spirit",
        normalized_title="smells like teen spirit",
        normalized_signature=uuid4().hex,
    )
    PgLogIdentityRepository(conn).upsert(identity)

    events = []
    for i in range(count):
        ev = LogEvent(
            id=uuid4(), identity_id=identity.id, playlist_id=playlist_id,
            played_at=datetime(2024, 1, 15, 10, i, 0),
        )
        PgLogEventRepository(conn).create(ev)
        events.append(ev)
    conn.commit()
    return artist, identity, events


class TestListPlaylists:
    def test_by_station(self, client, db_conn):
        station = _seed_station(db_conn)
        _seed_playlist(db_conn, station.id)
        resp = client.get(f"/api/v1/playlists?station_id={station.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["station_id"] == str(station.id)

    def test_requires_station_id(self, client):
        resp = client.get("/api/v1/playlists")
        assert resp.status_code == 422


class TestGetPlaylist:
    def test_found(self, client, db_conn):
        station = _seed_station(db_conn)
        playlist = _seed_playlist(db_conn, station.id)
        resp = client.get(f"/api/v1/playlists/{playlist.id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "test.csv"

    def test_not_found(self, client):
        resp = client.get(f"/api/v1/playlists/{uuid4()}")
        assert resp.status_code == 404


class TestPlaylistEvents:
    def test_paginated(self, client, db_conn):
        station = _seed_station(db_conn)
        playlist = _seed_playlist(db_conn, station.id)
        _seed_events(db_conn, playlist.id, count=5)
        resp = client.get(
            f"/api/v1/playlists/{playlist.id}/events?limit=2&offset=0"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2

    def test_default_pagination(self, client, db_conn):
        station = _seed_station(db_conn)
        playlist = _seed_playlist(db_conn, station.id)
        _seed_events(db_conn, playlist.id, count=3)
        resp = client.get(f"/api/v1/playlists/{playlist.id}/events")
        assert resp.status_code == 200
        assert resp.json()["total"] == 3


class TestBroadcastDays:
    def test_returns_dates(self, client, db_conn):
        station = _seed_station(db_conn)
        playlist = _seed_playlist(db_conn, station.id)
        bd_repo = PgBroadcastDayRepository(db_conn)
        bd_repo.get_or_create(station.id, date(2024, 1, 15))
        bd_repo.get_or_create(station.id, date(2024, 1, 16))
        db_conn.commit()

        resp = client.get(
            f"/api/v1/playlists/{playlist.id}/broadcast-days"
        )
        assert resp.status_code == 200
        dates = resp.json()
        assert len(dates) == 2
```

- [ ] **Step 1b: Run tests, confirm failures**

Run: `uv run pytest tests/routers/test_playlists.py -v`
Expected: All FAIL (router not implemented).

### Step 2 — Implement playlists router

- [ ] **Step 2a: Create `backend/routers/playlists.py`**

```python
# backend/routers/playlists.py
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


# ── Schemas ────────────────────────────────────────────────────────────

class PlaylistSummary(BaseModel):
    id: UUID
    name: str
    station_id: UUID | None
    content_hash: str
    ingested_at: datetime
    event_count: int


class PlaylistDetail(BaseModel):
    id: UUID
    name: str
    station_id: UUID | None
    content_hash: str
    ingested_at: datetime


class EventItem(BaseModel):
    id: UUID
    played_at: datetime
    artist_name: str
    title: str
    match_status: str
    match_tier: str | None


class PaginatedEvents(BaseModel):
    items: list[EventItem]
    total: int


# ── Endpoints ──────────────────────────────────────────────────────────

@router.get("")
async def list_playlists(
    station_id: UUID = Query(...),
    conn: DbConn = ...,
    _token: Token = ...,
) -> list[PlaylistSummary]:
    cur = await conn.execute(
        """SELECT p.*,
                  COUNT(le.id) AS event_count
           FROM playlists p
           LEFT JOIN log_events le ON le.playlist_id = p.id
           WHERE p.station_id = %s
           GROUP BY p.id
           ORDER BY p.ingested_at DESC""",
        (station_id,),
    )
    rows = await cur.fetchall()
    return [
        PlaylistSummary(
            id=r["id"], name=r["name"], station_id=r["station_id"],
            content_hash=r["content_hash"], ingested_at=r["ingested_at"],
            event_count=r["event_count"],
        )
        for r in rows
    ]


@router.get("/{playlist_id}")
async def get_playlist(
    playlist_id: UUID, conn: DbConn, _token: Token,
) -> PlaylistDetail:
    cur = await conn.execute(
        "SELECT * FROM playlists WHERE id = %s", (playlist_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return PlaylistDetail(
        id=row["id"], name=row["name"], station_id=row["station_id"],
        content_hash=row["content_hash"], ingested_at=row["ingested_at"],
    )


@router.get("/{playlist_id}/events")
async def list_events(
    playlist_id: UUID,
    conn: DbConn,
    _token: Token,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PaginatedEvents:
    # Verify playlist exists
    cur = await conn.execute(
        "SELECT id FROM playlists WHERE id = %s", (playlist_id,)
    )
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Playlist not found")

    # Total count
    cur = await conn.execute(
        "SELECT COUNT(*) AS cnt FROM log_events WHERE playlist_id = %s",
        (playlist_id,),
    )
    total_row = await cur.fetchone()
    total = total_row["cnt"] if total_row else 0

    # Paginated events with joined identity + artist info
    cur = await conn.execute(
        """SELECT le.id, le.played_at,
                  la.original_name AS artist_name,
                  li.original_title AS title,
                  li.match_status,
                  li.match_tier
           FROM log_events le
           JOIN log_identities li ON li.id = le.identity_id
           JOIN log_artists la ON la.id = li.artist_id
           WHERE le.playlist_id = %s
           ORDER BY le.played_at
           LIMIT %s OFFSET %s""",
        (playlist_id, limit, offset),
    )
    rows = await cur.fetchall()
    return PaginatedEvents(
        items=[
            EventItem(
                id=r["id"], played_at=r["played_at"],
                artist_name=r["artist_name"], title=r["title"],
                match_status=r["match_status"],
                match_tier=r.get("match_tier"),
            )
            for r in rows
        ],
        total=total,
    )


@router.get("/{playlist_id}/broadcast-days")
async def list_broadcast_days(
    playlist_id: UUID, conn: DbConn, _token: Token,
) -> list[str]:
    # Get station_id from playlist
    cur = await conn.execute(
        "SELECT station_id FROM playlists WHERE id = %s", (playlist_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    station_id = row["station_id"]
    if station_id is None:
        return []

    cur = await conn.execute(
        """SELECT broadcast_date FROM broadcast_days
           WHERE station_id = %s ORDER BY broadcast_date""",
        (station_id,),
    )
    rows = await cur.fetchall()
    return [r["broadcast_date"].isoformat() for r in rows]
```

- [ ] **Step 2b: Register in `v1.py`**

Add to `backend/routers/v1.py`:

```python
from backend.routers import ingestion, library, playlists, stations

# ... after existing include_router calls:
router.include_router(playlists.router, prefix="/playlists", tags=["playlists"])
```

- [ ] **Step 2c: Run tests**

Run: `uv run pytest tests/routers/test_playlists.py -v`
Expected: All PASS.

- [ ] **Step 2d: Full suite + type check**

Run: `uv run pytest tests/ -x -q && uv run mypy --strict backend/ && uv run ruff check backend/`
Expected: All pass.

- [ ] **Step 2e: Commit**

```bash
git add backend/routers/playlists.py backend/routers/v1.py tests/routers/test_playlists.py
git commit -m "feat: playlists router — list, detail, paginated events, broadcast days"
```

---

## Task 3: Library — Status, Artists, Works, Masters, Format Overrides

**Files:**
- Create: `backend/db/repositories/format_overrides.py`, `tests/routers/test_library.py`, `tests/integration/test_pg_format_overrides_repo.py`
- Modify: `backend/routers/library.py`, `backend/services/repository_factory.py`

### Step 1 — PgFormatOverrideRepository

- [ ] **Step 1a: Write integration test**

```python
# tests/integration/test_pg_format_overrides_repo.py
from __future__ import annotations

from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.format_overrides import PgFormatOverrideRepository
from backend.domain.models import FormatOverride


class TestPgFormatOverrideRepository:
    def test_create_and_list(self, migrated_db: str):
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgFormatOverrideRepository(conn)
            fo = FormatOverride(
                id=uuid4(), work_id="test-work-mbid",
                format_name="CHR", preferred_file_id=uuid4(),
                notes="radio edit preferred",
            )
            result = repo.create(fo)
            conn.commit()
            assert result.work_id == "test-work-mbid"

            items = repo.list_by_work("test-work-mbid")
            assert len(items) == 1
            assert items[0].format_name == "CHR"

    def test_get(self, migrated_db: str):
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgFormatOverrideRepository(conn)
            fo = FormatOverride(
                id=uuid4(), work_id="w1", format_name="AC",
                preferred_file_id=uuid4(),
            )
            repo.create(fo)
            conn.commit()
            assert repo.get("w1", "AC") is not None
            assert repo.get("w1", "CHR") is None

    def test_delete(self, migrated_db: str):
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgFormatOverrideRepository(conn)
            fo = FormatOverride(
                id=uuid4(), work_id="w2", format_name="CHR",
                preferred_file_id=uuid4(),
            )
            repo.create(fo)
            conn.commit()
            repo.delete(fo.id)
            conn.commit()
            assert repo.get("w2", "CHR") is None
```

- [ ] **Step 1b: Implement PgFormatOverrideRepository**

```python
# backend/db/repositories/format_overrides.py
from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.domain.models import FormatOverride
from backend.repositories.format_overrides import FormatOverrideRepository


class PgFormatOverrideRepository(FormatOverrideRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> FormatOverride:
        return FormatOverride(
            id=row["id"],
            work_id=row["work_id"],
            format_name=row["format_name"],
            preferred_file_id=row["preferred_file_id"],
            notes=row.get("notes"),
            created_at=row["created_at"],
        )

    def create(self, override: FormatOverride) -> FormatOverride:
        self._conn.execute(
            """INSERT INTO format_overrides
               (id, work_id, format_name, preferred_file_id, notes)
               VALUES (%s, %s, %s, %s, %s)""",
            (override.id, override.work_id, override.format_name,
             override.preferred_file_id, override.notes),
        )
        row = self._conn.execute(
            "SELECT * FROM format_overrides WHERE id = %s", (override.id,)
        ).fetchone()
        assert row is not None
        return self._row_to_model(row)

    def get(self, work_id: str, format_name: str) -> FormatOverride | None:
        row = self._conn.execute(
            "SELECT * FROM format_overrides WHERE work_id = %s AND format_name = %s",
            (work_id, format_name),
        ).fetchone()
        return self._row_to_model(row) if row else None

    def list_by_work(self, work_id: str) -> list[FormatOverride]:
        rows = self._conn.execute(
            "SELECT * FROM format_overrides WHERE work_id = %s ORDER BY format_name",
            (work_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def delete(self, id: UUID) -> None:
        self._conn.execute("DELETE FROM format_overrides WHERE id = %s", (id,))
```

- [ ] **Step 1c: Update RepositoryFactory**

Add to `backend/services/repository_factory.py`:

```python
from backend.db.repositories.format_overrides import PgFormatOverrideRepository
```

And in `__init__`:

```python
self.format_overrides = PgFormatOverrideRepository(conn)
```

- [ ] **Step 1d: Run repo tests**

Run: `uv run pytest tests/integration/test_pg_format_overrides_repo.py -v`
Expected: All PASS.

- [ ] **Step 1e: Commit**

```bash
git add backend/db/repositories/format_overrides.py backend/services/repository_factory.py tests/integration/test_pg_format_overrides_repo.py
git commit -m "feat: PgFormatOverrideRepository + integration tests"
```

### Step 2 — Library GET endpoints

- [ ] **Step 2a: Write library tests**

```python
# tests/routers/test_library.py
from __future__ import annotations

from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.recordings import PgRecordingRepository
from backend.db.repositories.song_masters import PgSongMasterRepository
from backend.db.repositories.works import PgWorkRepository
from backend.domain.enums import EnrichmentStatus, SelectionMethod
from backend.domain.models import Artist, LibraryFile, Recording, SongMaster, Work


def _seed_canonical(conn: psycopg.Connection):
    """Create artist → work → recording → library_file chain."""
    artist = Artist(id="art-001", name="Nirvana", sort_name="Nirvana")
    PgArtistRepository(conn).upsert(artist)

    work = Work(id="work-001", title="Smells Like Teen Spirit", artist_id="art-001")
    PgWorkRepository(conn).upsert(work)

    rec = Recording(
        id="rec-001", title="Smells Like Teen Spirit", work_id="work-001",
        duration_ms=301000,
    )
    PgRecordingRepository(conn).upsert(rec)

    lf = LibraryFile(
        id=uuid4(),
        file_path="/music/Nirvana/Nevermind/01-smells-like-teen-spirit.flac",
        file_hash="abc123", format="flac",
        recording_id="rec-001", recording_mbid="rec-001",
        artist_mbid="art-001", track_title="Smells Like Teen Spirit",
        enrichment_status=EnrichmentStatus.ENRICHED,
        bitrate=1411, duration_ms=301000,
    )
    PgLibraryFileRepository(conn).upsert(lf)
    conn.commit()
    return artist, work, rec, lf


class TestLibraryStatus:
    def test_returns_counts(self, client, db_conn):
        _seed_canonical(db_conn)
        resp = client.get("/api/v1/library/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_files"] == 1
        assert "flac" in data["by_format"]
        assert data["by_format"]["flac"] == 1
        assert "enriched" in data["by_enrichment"]


class TestLibraryArtists:
    def test_paginated(self, client, db_conn):
        _seed_canonical(db_conn)
        resp = client.get("/api/v1/library/artists?limit=10&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert data["items"][0]["name"] == "Nirvana"

    def test_search(self, client, db_conn):
        _seed_canonical(db_conn)
        resp = client.get("/api/v1/library/artists?search=nirv")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

        resp = client.get("/api/v1/library/artists?search=zzz")
        assert resp.json()["total"] == 0


class TestArtistDetail:
    def test_found(self, client, db_conn):
        artist, work, _, _ = _seed_canonical(db_conn)
        resp = client.get(f"/api/v1/library/artists/{artist.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Nirvana"
        assert len(data["works"]) == 1
        assert data["works"][0]["title"] == "Smells Like Teen Spirit"

    def test_not_found(self, client):
        resp = client.get("/api/v1/library/artists/nonexistent-mbid")
        assert resp.status_code == 404


class TestWorkDetail:
    def test_found(self, client, db_conn):
        _, work, _, lf = _seed_canonical(db_conn)
        resp = client.get(f"/api/v1/library/works/{work.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Smells Like Teen Spirit"
        assert len(data["recordings"]) == 1
        assert len(data["recordings"][0]["files"]) == 1

    def test_not_found(self, client):
        resp = client.get("/api/v1/library/works/nonexistent")
        assert resp.status_code == 404


class TestSetMaster:
    def test_set_manual_master(self, client, db_conn):
        _, work, _, lf = _seed_canonical(db_conn)
        resp = client.put(
            f"/api/v1/library/works/{work.id}/master",
            json={"preferred_file_id": str(lf.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["selection_method"] == "manual"

    def test_revert_to_auto(self, client, db_conn):
        _, work, _, lf = _seed_canonical(db_conn)
        # First set manual
        client.put(
            f"/api/v1/library/works/{work.id}/master",
            json={"preferred_file_id": str(lf.id)},
        )
        # Then revert
        resp = client.delete(f"/api/v1/library/works/{work.id}/master")
        assert resp.status_code == 204
```

- [ ] **Step 2b: Run tests, confirm failures**

Run: `uv run pytest tests/routers/test_library.py -v`
Expected: All FAIL.

### Step 3 — Implement library GET endpoints

- [ ] **Step 3a: Expand `backend/routers/library.py`**

Replace the entire contents of `backend/routers/library.py`:

```python
# backend/routers/library.py
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel

from backend.config import get_settings
from backend.dependencies import get_current_token, get_db_connection
from backend.domain.enums import SelectionMethod
from backend.domain.models import SongMaster
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.library_tasks import library_scan_task

router = APIRouter()

DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
Token = Annotated[str, Depends(get_current_token)]


# ── Schemas ────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    root_path: str


class LibraryStatus(BaseModel):
    total_files: int
    quarantine_count: int
    by_format: dict[str, int]
    by_enrichment: dict[str, int]


class ArtistSummary(BaseModel):
    id: str
    name: str
    sort_name: str
    disambiguation: str | None
    work_count: int
    file_count: int


class PaginatedArtists(BaseModel):
    items: list[ArtistSummary]
    total: int


class WorkSummary(BaseModel):
    id: str
    title: str
    recording_count: int
    has_master: bool


class ArtistDetail(BaseModel):
    id: str
    name: str
    sort_name: str
    disambiguation: str | None
    works: list[WorkSummary]


class FileInfo(BaseModel):
    id: UUID
    file_path: str
    format: str
    bitrate: int | None
    duration_ms: int | None
    track_title: str | None
    release_title: str | None
    enrichment_status: str


class RecordingDetail(BaseModel):
    id: str
    title: str
    version_type: str
    duration_ms: int | None
    files: list[FileInfo]


class FormatOverrideInfo(BaseModel):
    id: UUID
    format_name: str
    preferred_file_id: UUID
    notes: str | None
    created_at: datetime


class SongMasterInfo(BaseModel):
    id: UUID
    preferred_file_id: UUID
    selection_method: str
    score: int | None


class WorkDetail(BaseModel):
    id: str
    title: str
    artist_id: str
    recordings: list[RecordingDetail]
    song_master: SongMasterInfo | None
    format_overrides: list[FormatOverrideInfo]


class SetMasterBody(BaseModel):
    preferred_file_id: UUID


class SongMasterResponse(BaseModel):
    id: UUID
    work_id: str
    preferred_file_id: UUID
    selection_method: str
    score: int | None


class CreateOverrideBody(BaseModel):
    format_name: str
    preferred_file_id: UUID
    notes: str | None = None


class FormatOverrideResponse(BaseModel):
    id: UUID
    work_id: str
    format_name: str
    preferred_file_id: UUID
    notes: str | None
    created_at: datetime


# ── Endpoints ──────────────────────────────────────────────────────────

@router.post("/scan", status_code=status.HTTP_202_ACCEPTED)
async def scan_library(body: ScanRequest) -> dict[str, str]:
    """Enqueue a background library scan for the given directory."""
    library_scan_task(body.root_path)
    return {"status": "accepted", "message": f"Library scan queued for {body.root_path}"}


@router.get("/status")
async def library_status(conn: DbConn, _token: Token) -> LibraryStatus:
    cur = await conn.execute("SELECT COUNT(*) AS cnt FROM library_files")
    total = (await cur.fetchone() or {}).get("cnt", 0)

    cur = await conn.execute("SELECT COUNT(*) AS cnt FROM library_quarantine")
    qcount = (await cur.fetchone() or {}).get("cnt", 0)

    cur = await conn.execute(
        """SELECT format, COUNT(*) AS cnt FROM library_files
           GROUP BY format ORDER BY cnt DESC"""
    )
    by_format = {r["format"]: r["cnt"] for r in await cur.fetchall()}

    cur = await conn.execute(
        """SELECT enrichment_status, COUNT(*) AS cnt FROM library_files
           GROUP BY enrichment_status ORDER BY cnt DESC"""
    )
    by_enrichment = {r["enrichment_status"]: r["cnt"] for r in await cur.fetchall()}

    return LibraryStatus(
        total_files=total, quarantine_count=qcount,
        by_format=by_format, by_enrichment=by_enrichment,
    )


@router.get("/artists")
async def list_artists(
    conn: DbConn, _token: Token,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None),
) -> PaginatedArtists:
    where = ""
    params: list[Any] = []
    if search:
        where = "WHERE LOWER(a.name) LIKE %s"
        params.append(f"%{search.lower()}%")

    cur = await conn.execute(
        f"SELECT COUNT(*) AS cnt FROM artists a {where}", params,
    )
    total = (await cur.fetchone() or {}).get("cnt", 0)

    cur = await conn.execute(
        f"""SELECT a.*,
                   COUNT(DISTINCT w.id) AS work_count,
                   COUNT(DISTINCT lf.id) AS file_count
            FROM artists a
            LEFT JOIN works w ON w.artist_id = a.id
            LEFT JOIN recordings r ON r.work_id = w.id
            LEFT JOIN library_files lf ON lf.recording_id = r.id
            {where}
            GROUP BY a.id
            ORDER BY a.sort_name
            LIMIT %s OFFSET %s""",
        [*params, limit, offset],
    )
    rows = await cur.fetchall()

    return PaginatedArtists(
        items=[
            ArtistSummary(
                id=r["id"], name=r["name"], sort_name=r["sort_name"],
                disambiguation=r.get("disambiguation"),
                work_count=r["work_count"], file_count=r["file_count"],
            )
            for r in rows
        ],
        total=total,
    )


@router.get("/artists/{artist_id}")
async def get_artist(
    artist_id: str, conn: DbConn, _token: Token,
) -> ArtistDetail:
    cur = await conn.execute(
        "SELECT * FROM artists WHERE id = %s", (artist_id,)
    )
    artist = await cur.fetchone()
    if artist is None:
        raise HTTPException(status_code=404, detail="Artist not found")

    cur = await conn.execute(
        """SELECT w.*,
                  COUNT(DISTINCT r.id) AS recording_count,
                  (sm.id IS NOT NULL) AS has_master
           FROM works w
           LEFT JOIN recordings r ON r.work_id = w.id
           LEFT JOIN song_masters sm ON sm.work_id = w.id
           WHERE w.artist_id = %s
           GROUP BY w.id, sm.id
           ORDER BY w.title""",
        (artist_id,),
    )
    works = await cur.fetchall()

    return ArtistDetail(
        id=artist["id"], name=artist["name"],
        sort_name=artist["sort_name"],
        disambiguation=artist.get("disambiguation"),
        works=[
            WorkSummary(
                id=w["id"], title=w["title"],
                recording_count=w["recording_count"],
                has_master=bool(w["has_master"]),
            )
            for w in works
        ],
    )


@router.get("/works/{work_id}")
async def get_work(
    work_id: str, conn: DbConn, _token: Token,
) -> WorkDetail:
    cur = await conn.execute(
        "SELECT * FROM works WHERE id = %s", (work_id,)
    )
    work = await cur.fetchone()
    if work is None:
        raise HTTPException(status_code=404, detail="Work not found")

    # Recordings
    cur = await conn.execute(
        "SELECT * FROM recordings WHERE work_id = %s ORDER BY title",
        (work_id,),
    )
    recording_rows = await cur.fetchall()

    recordings = []
    for rec in recording_rows:
        cur = await conn.execute(
            """SELECT * FROM library_files WHERE recording_id = %s
               ORDER BY file_path""",
            (rec["id"],),
        )
        file_rows = await cur.fetchall()
        recordings.append(RecordingDetail(
            id=rec["id"], title=rec["title"],
            version_type=rec.get("version_type", "ORIGINAL"),
            duration_ms=rec.get("duration_ms"),
            files=[
                FileInfo(
                    id=f["id"], file_path=f["file_path"], format=f["format"],
                    bitrate=f.get("bitrate"), duration_ms=f.get("duration_ms"),
                    track_title=f.get("track_title"),
                    release_title=f.get("release_title"),
                    enrichment_status=f["enrichment_status"],
                )
                for f in file_rows
            ],
        ))

    # Song master
    cur = await conn.execute(
        "SELECT * FROM song_masters WHERE work_id = %s", (work_id,)
    )
    master_row = await cur.fetchone()
    song_master = None
    if master_row:
        song_master = SongMasterInfo(
            id=master_row["id"],
            preferred_file_id=master_row["preferred_file_id"],
            selection_method=master_row["selection_method"],
            score=master_row.get("score"),
        )

    # Format overrides
    cur = await conn.execute(
        "SELECT * FROM format_overrides WHERE work_id = %s ORDER BY format_name",
        (work_id,),
    )
    override_rows = await cur.fetchall()

    return WorkDetail(
        id=work["id"], title=work["title"], artist_id=work["artist_id"],
        recordings=recordings, song_master=song_master,
        format_overrides=[
            FormatOverrideInfo(
                id=o["id"], format_name=o["format_name"],
                preferred_file_id=o["preferred_file_id"],
                notes=o.get("notes"), created_at=o["created_at"],
            )
            for o in override_rows
        ],
    )


@router.put("/works/{work_id}/master")
async def set_master(
    work_id: str, body: SetMasterBody, conn: DbConn, _token: Token,
) -> SongMasterResponse:
    cur = await conn.execute(
        "SELECT * FROM works WHERE id = %s", (work_id,)
    )
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Work not found")

    cur = await conn.execute(
        "SELECT * FROM song_masters WHERE work_id = %s", (work_id,)
    )
    existing = await cur.fetchone()

    master_id = existing["id"] if existing else uuid4()
    await conn.execute(
        """INSERT INTO song_masters (id, work_id, preferred_file_id, selection_method, score, updated_at)
           VALUES (%s, %s, %s, 'manual', NULL, now())
           ON CONFLICT (work_id) DO UPDATE SET
             preferred_file_id = EXCLUDED.preferred_file_id,
             selection_method = EXCLUDED.selection_method,
             score = EXCLUDED.score,
             updated_at = EXCLUDED.updated_at""",
        (master_id, work_id, body.preferred_file_id),
    )

    cur = await conn.execute(
        "SELECT * FROM song_masters WHERE work_id = %s", (work_id,)
    )
    row = await cur.fetchone()
    assert row is not None
    return SongMasterResponse(
        id=row["id"], work_id=row["work_id"],
        preferred_file_id=row["preferred_file_id"],
        selection_method=row["selection_method"], score=row.get("score"),
    )


@router.delete("/works/{work_id}/master", status_code=status.HTTP_204_NO_CONTENT)
async def revert_master(
    work_id: str, conn: DbConn, _token: Token,
) -> None:
    """Delete the song master for this work (auto-selection will recalculate)."""
    await conn.execute(
        "DELETE FROM song_masters WHERE work_id = %s", (work_id,)
    )


@router.post("/works/{work_id}/format-overrides",
             status_code=status.HTTP_201_CREATED)
async def create_format_override(
    work_id: str, body: CreateOverrideBody, conn: DbConn, _token: Token,
) -> FormatOverrideResponse:
    cur = await conn.execute(
        "SELECT id FROM works WHERE id = %s", (work_id,)
    )
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Work not found")

    override_id = uuid4()
    await conn.execute(
        """INSERT INTO format_overrides (id, work_id, format_name, preferred_file_id, notes)
           VALUES (%s, %s, %s, %s, %s)""",
        (override_id, work_id, body.format_name, body.preferred_file_id, body.notes),
    )
    cur = await conn.execute(
        "SELECT * FROM format_overrides WHERE id = %s", (override_id,)
    )
    row = await cur.fetchone()
    assert row is not None
    return FormatOverrideResponse(
        id=row["id"], work_id=row["work_id"], format_name=row["format_name"],
        preferred_file_id=row["preferred_file_id"],
        notes=row.get("notes"), created_at=row["created_at"],
    )


@router.delete("/works/{work_id}/format-overrides/{override_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def delete_format_override(
    work_id: str, override_id: UUID, conn: DbConn, _token: Token,
) -> None:
    cur = await conn.execute(
        "SELECT id FROM format_overrides WHERE id = %s AND work_id = %s",
        (override_id, work_id),
    )
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Format override not found")
    await conn.execute(
        "DELETE FROM format_overrides WHERE id = %s", (override_id,)
    )
```

- [ ] **Step 3b: Run tests**

Run: `uv run pytest tests/routers/test_library.py -v`
Expected: All PASS.

- [ ] **Step 3c: Full suite + type check**

Run: `uv run pytest tests/ -x -q && uv run mypy --strict backend/ && uv run ruff check backend/`
Expected: All pass.

- [ ] **Step 3d: Commit**

```bash
git add backend/routers/library.py backend/db/repositories/format_overrides.py backend/services/repository_factory.py tests/routers/test_library.py tests/integration/test_pg_format_overrides_repo.py
git commit -m "feat: library router — status, paginated artists, work detail, master toggle, format overrides"
```

---

## Task 4: Matching — Queue, Resolution, Re-run

**Files:**
- Create: `backend/routers/matching.py`, `tests/routers/test_matching.py`
- Modify: `backend/routers/v1.py`

### Step 1 — Write matching tests

- [ ] **Step 1a: Create test file**

```python
# tests/routers/test_matching.py
from __future__ import annotations

from uuid import uuid4

import psycopg

from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.log_artists import PgLogArtistRepository
from backend.db.repositories.log_events import PgLogEventRepository
from backend.db.repositories.log_identities import PgLogIdentityRepository
from backend.db.repositories.matches import PgMatchRepository
from backend.db.repositories.playlists import PgPlaylistRepository
from backend.db.repositories.recordings import PgRecordingRepository
from backend.db.repositories.stations import PgStationRepository
from backend.db.repositories.works import PgWorkRepository
from backend.domain.enums import (
    EnrichmentStatus,
    MatchStatus,
    MatchTier,
)
from backend.domain.models import (
    Artist,
    LibraryFile,
    LogArtist,
    LogEvent,
    LogIdentity,
    Match,
    Playlist,
    Recording,
    Station,
    Work,
)


def _seed_review_data(conn: psycopg.Connection):
    """Create a log_artist with NEEDS_REVIEW status and child identities."""
    # Station + playlist for events
    station = Station(id=uuid4(), call_letters="KAZR-FM")
    PgStationRepository(conn).create(station)
    playlist = Playlist(
        id=uuid4(), name="t.csv", content_hash=uuid4().hex,
        station_id=station.id,
    )
    PgPlaylistRepository(conn).create(playlist)

    # Log artist needing review
    la = LogArtist(
        id=uuid4(), original_name="Nirvana", normalized_name="nirvana",
        match_status=MatchStatus.NEEDS_REVIEW,
        artist_candidates=[
            {"mbid": "art-001", "name": "Nirvana", "score": 92},
            {"mbid": "art-002", "name": "Nirvana (tribute)", "score": 85},
        ],
    )
    PgLogArtistRepository(conn).upsert(la)

    # Identity under this artist
    li = LogIdentity(
        id=uuid4(), artist_id=la.id,
        original_title="Smells Like Teen Spirit",
        normalized_title="smells like teen spirit",
        normalized_signature=uuid4().hex,
        match_status=MatchStatus.PENDING,
    )
    PgLogIdentityRepository(conn).upsert(li)

    # Event linking identity to playlist
    from datetime import datetime as _dt

    ev = LogEvent(
        id=uuid4(), identity_id=li.id, playlist_id=playlist.id,
        played_at=_dt.utcnow(),
    )
    PgLogEventRepository(conn).create(ev)

    # Canonical artist for resolution target
    canonical = Artist(id="art-001", name="Nirvana", sort_name="Nirvana")
    PgArtistRepository(conn).upsert(canonical)

    # Library file for identity resolution
    work = Work(id="work-001", title="Smells Like Teen Spirit", artist_id="art-001")
    PgWorkRepository(conn).upsert(work)
    rec = Recording(id="rec-001", title="Smells Like Teen Spirit", work_id="work-001")
    PgRecordingRepository(conn).upsert(rec)
    lf = LibraryFile(
        id=uuid4(), file_path="/music/nirvana/01.flac", file_hash="abc",
        format="flac", recording_id="rec-001",
        enrichment_status=EnrichmentStatus.ENRICHED,
    )
    PgLibraryFileRepository(conn).upsert(lf)
    conn.commit()
    return la, li, canonical, lf, playlist


class TestMatchingQueue:
    def test_returns_artists_needing_review(self, client, db_conn):
        la, li, *_ = _seed_review_data(db_conn)
        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        artist = data["items"][0]
        assert artist["original_name"] == "Nirvana"
        assert artist["match_status"] == "NEEDS_REVIEW"
        assert len(artist["identities"]) >= 1

    def test_empty_queue(self, client):
        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestResolveArtist:
    def test_manual_match(self, client, db_conn):
        la, li, canonical, *_ = _seed_review_data(db_conn)
        resp = client.post(
            f"/api/v1/matching/artists/{la.id}/resolve",
            json={
                "match_status": "MAN_MATCHED",
                "target_artist_id": canonical.id,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["match_status"] == "MAN_MATCHED"

    def test_manual_reject_cascades(self, client, db_conn):
        la, li, *_ = _seed_review_data(db_conn)
        resp = client.post(
            f"/api/v1/matching/artists/{la.id}/resolve",
            json={"match_status": "MAN_REJECTED"},
        )
        assert resp.status_code == 200
        # Verify child identity was cascaded to AUTO_REJECTED
        from psycopg.rows import dict_row
        with psycopg.connect(db_conn.info.dsn, row_factory=dict_row) as conn2:
            row = conn2.execute(
                "SELECT match_status FROM log_identities WHERE id = %s",
                (li.id,),
            ).fetchone()
            assert row is not None
            assert row["match_status"] == "AUTO_REJECTED"

    def test_not_found(self, client):
        resp = client.post(
            f"/api/v1/matching/artists/{uuid4()}/resolve",
            json={"match_status": "MAN_REJECTED"},
        )
        assert resp.status_code == 404


class TestResolveIdentity:
    def test_manual_match(self, client, db_conn):
        la, li, _, lf, _ = _seed_review_data(db_conn)
        # First resolve artist so identity is eligible
        client.post(
            f"/api/v1/matching/artists/{la.id}/resolve",
            json={"match_status": "MAN_MATCHED", "target_artist_id": "art-001"},
        )
        resp = client.post(
            f"/api/v1/matching/identities/{li.id}/resolve",
            json={
                "match_status": "MAN_MATCHED",
                "library_file_id": str(lf.id),
            },
        )
        assert resp.status_code == 200
        assert resp.json()["match_status"] == "MAN_MATCHED"


class TestMatchingRun:
    def test_accepted(self, client):
        resp = client.post("/api/v1/matching/run")
        assert resp.status_code == 202
```

- [ ] **Step 1b: Run tests, confirm failures**

Run: `uv run pytest tests/routers/test_matching.py -v`
Expected: All FAIL.

### Step 2 — Implement matching router

- [ ] **Step 2a: Create `backend/routers/matching.py`**

```python
# backend/routers/matching.py
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.dependencies import get_current_token, get_db_connection
from backend.domain.enums import MatchStatus, MatchTier, TargetType

router = APIRouter()

DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
Token = Annotated[str, Depends(get_current_token)]


# ── Schemas ────────────────────────────────────────────────────────────

class QueueIdentity(BaseModel):
    id: UUID
    original_title: str
    normalized_title: str
    match_status: str
    match_tier: str | None


class QueueArtist(BaseModel):
    id: UUID
    original_name: str
    normalized_name: str
    match_status: str
    candidates: list[dict[str, Any]] | None
    identities: list[QueueIdentity]


class MatchingQueue(BaseModel):
    items: list[QueueArtist]
    total: int


class ArtistResolution(BaseModel):
    match_status: str  # MAN_MATCHED or MAN_REJECTED
    target_artist_id: str | None = None


class IdentityResolution(BaseModel):
    match_status: str  # MAN_MATCHED or MAN_REJECTED
    library_file_id: UUID | None = None


class ResolveResult(BaseModel):
    id: UUID
    match_status: str


# ── Endpoints ──────────────────────────────────────────────────────────

@router.get("/queue")
async def get_queue(
    conn: DbConn, _token: Token,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> MatchingQueue:
    # Count artists needing review
    cur = await conn.execute(
        """SELECT COUNT(*) AS cnt FROM log_artists
           WHERE match_status IN ('NEEDS_REVIEW', 'PENDING')"""
    )
    total = (await cur.fetchone() or {}).get("cnt", 0)

    # Paginated artists
    cur = await conn.execute(
        """SELECT * FROM log_artists
           WHERE match_status IN ('NEEDS_REVIEW', 'PENDING')
           ORDER BY original_name
           LIMIT %s OFFSET %s""",
        (limit, offset),
    )
    artist_rows = await cur.fetchall()

    if not artist_rows:
        return MatchingQueue(items=[], total=total)

    # Batch-fetch identities for these artists
    artist_ids = [r["id"] for r in artist_rows]
    cur = await conn.execute(
        """SELECT * FROM log_identities
           WHERE artist_id = ANY(%s)
           ORDER BY artist_id, original_title""",
        (artist_ids,),
    )
    identity_rows = await cur.fetchall()

    # Group identities by artist_id
    identities_by_artist: dict[UUID, list[dict[str, Any]]] = {}
    for row in identity_rows:
        identities_by_artist.setdefault(row["artist_id"], []).append(row)

    import json

    items = []
    for a in artist_rows:
        candidates = a.get("artist_candidates")
        if isinstance(candidates, str):
            candidates = json.loads(candidates)

        idents = identities_by_artist.get(a["id"], [])
        items.append(QueueArtist(
            id=a["id"],
            original_name=a["original_name"],
            normalized_name=a["normalized_name"],
            match_status=a["match_status"],
            candidates=candidates,
            identities=[
                QueueIdentity(
                    id=i["id"],
                    original_title=i["original_title"],
                    normalized_title=i["normalized_title"],
                    match_status=i["match_status"],
                    match_tier=i.get("match_tier"),
                )
                for i in idents
            ],
        ))

    return MatchingQueue(items=items, total=total)


@router.post("/artists/{artist_id}/resolve")
async def resolve_artist(
    artist_id: UUID, body: ArtistResolution, conn: DbConn, _token: Token,
) -> ResolveResult:
    cur = await conn.execute(
        "SELECT * FROM log_artists WHERE id = %s", (artist_id,)
    )
    artist = await cur.fetchone()
    if artist is None:
        raise HTTPException(status_code=404, detail="Log artist not found")

    new_status = MatchStatus(body.match_status)

    if new_status == MatchStatus.MAN_MATCHED:
        if body.target_artist_id is None:
            raise HTTPException(
                status_code=422,
                detail="target_artist_id required for MAN_MATCHED",
            )
        # Update artist match status
        await conn.execute(
            "UPDATE log_artists SET match_status = %s WHERE id = %s",
            (new_status.value, artist_id),
        )
        # Create match row
        await conn.execute(
            """INSERT INTO matches
               (id, artist_id, target_id, target_type, confidence_score, match_tier, trace_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (uuid4(), artist_id, body.target_artist_id,
             TargetType.ARTIST.value, 1.0, MatchTier.MANUAL.value, None),
        )

    elif new_status == MatchStatus.MAN_REJECTED:
        # Update artist
        await conn.execute(
            "UPDATE log_artists SET match_status = %s WHERE id = %s",
            (new_status.value, artist_id),
        )
        # Cascade: reject all child identities
        await conn.execute(
            """UPDATE log_identities
               SET match_status = 'AUTO_REJECTED'
               WHERE artist_id = %s AND match_status NOT IN ('MAN_MATCHED', 'MAN_REJECTED')""",
            (artist_id,),
        )
    else:
        raise HTTPException(
            status_code=422,
            detail="match_status must be MAN_MATCHED or MAN_REJECTED",
        )

    return ResolveResult(id=artist_id, match_status=new_status.value)


@router.post("/identities/{identity_id}/resolve")
async def resolve_identity(
    identity_id: UUID, body: IdentityResolution, conn: DbConn, _token: Token,
) -> ResolveResult:
    cur = await conn.execute(
        "SELECT * FROM log_identities WHERE id = %s", (identity_id,)
    )
    identity = await cur.fetchone()
    if identity is None:
        raise HTTPException(status_code=404, detail="Log identity not found")

    new_status = MatchStatus(body.match_status)

    if new_status == MatchStatus.MAN_MATCHED:
        if body.library_file_id is None:
            raise HTTPException(
                status_code=422,
                detail="library_file_id required for MAN_MATCHED",
            )
        # Update identity match status
        await conn.execute(
            "UPDATE log_identities SET match_status = %s, match_tier = %s WHERE id = %s",
            (new_status.value, MatchTier.MANUAL.value, identity_id),
        )
        # Delete any existing match for this identity
        await conn.execute(
            "DELETE FROM matches WHERE identity_id = %s", (identity_id,)
        )
        # Create new match
        await conn.execute(
            """INSERT INTO matches
               (id, identity_id, library_file_id, target_id, target_type,
                confidence_score, match_tier, trace_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (uuid4(), identity_id, body.library_file_id,
             str(body.library_file_id), TargetType.LIBRARY_FILE.value,
             1.0, MatchTier.MANUAL.value, None),
        )

    elif new_status == MatchStatus.MAN_REJECTED:
        await conn.execute(
            "UPDATE log_identities SET match_status = %s, match_tier = %s WHERE id = %s",
            (new_status.value, MatchTier.MANUAL.value, identity_id),
        )
        # Remove any existing match
        await conn.execute(
            "DELETE FROM matches WHERE identity_id = %s", (identity_id,)
        )
    else:
        raise HTTPException(
            status_code=422,
            detail="match_status must be MAN_MATCHED or MAN_REJECTED",
        )

    return ResolveResult(id=identity_id, match_status=new_status.value)


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def rerun_matching(conn: DbConn, _token: Token) -> dict[str, str]:
    """Re-run the matching pipeline on all playlists with unresolved identities."""
    from backend.tasks.artist_matching_tasks import artist_matching_task

    # Find all playlists that have unresolved artists
    cur = await conn.execute(
        """SELECT DISTINCT le.playlist_id
           FROM log_events le
           JOIN log_identities li ON li.id = le.identity_id
           JOIN log_artists la ON la.id = li.artist_id
           WHERE la.match_status IN ('PENDING', 'NEEDS_REVIEW')"""
    )
    rows = await cur.fetchall()
    playlist_ids = [r["playlist_id"] for r in rows]

    for pid in playlist_ids:
        artist_matching_task(str(pid))

    return {
        "status": "accepted",
        "message": f"Matching re-run queued for {len(playlist_ids)} playlist(s)",
    }
```

- [ ] **Step 2b: Register in `v1.py`**

Add matching to `backend/routers/v1.py`:

```python
from backend.routers import ingestion, library, matching, playlists, stations

# Add:
router.include_router(matching.router, prefix="/matching", tags=["matching"])
```

- [ ] **Step 2c: Run tests**

Run: `uv run pytest tests/routers/test_matching.py -v`
Expected: All PASS.

- [ ] **Step 2d: Full suite + type check**

Run: `uv run pytest tests/ -x -q && uv run mypy --strict backend/ && uv run ruff check backend/`
Expected: All pass.

- [ ] **Step 2e: Commit**

```bash
git add backend/routers/matching.py backend/routers/v1.py tests/routers/test_matching.py
git commit -m "feat: matching router — review queue, artist/identity resolution, re-run trigger"
```

---

## Task 5: Settings — PgSettingsRepository + Get/Put Endpoints

**Files:**
- Create: `backend/db/repositories/settings.py`, `tests/integration/test_pg_settings_repo.py`
- Create: `backend/routers/settings.py`, `tests/routers/test_settings.py`
- Modify: `backend/services/repository_factory.py`, `backend/routers/v1.py`

### Step 1 — PgSettingsRepository

- [ ] **Step 1a: Write integration test**

```python
# tests/integration/test_pg_settings_repo.py
from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.settings import PgSettingsRepository


class TestPgSettingsRepository:
    def test_set_and_get(self, migrated_db: str):
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSettingsRepository(conn)
            repo.set("library_root", "/music")
            conn.commit()
            assert repo.get("library_root") == "/music"

    def test_get_missing(self, migrated_db: str):
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSettingsRepository(conn)
            assert repo.get("nonexistent") is None

    def test_get_all(self, migrated_db: str):
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSettingsRepository(conn)
            repo.set("key_a", "val_a")
            repo.set("key_b", "val_b")
            conn.commit()
            result = repo.get_all()
            assert result["key_a"] == "val_a"
            assert result["key_b"] == "val_b"

    def test_overwrite(self, migrated_db: str):
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSettingsRepository(conn)
            repo.set("key", "old")
            repo.set("key", "new")
            conn.commit()
            assert repo.get("key") == "new"
```

- [ ] **Step 1b: Implement PgSettingsRepository**

```python
# backend/db/repositories/settings.py
from __future__ import annotations

from typing import Any

import psycopg

from backend.repositories.settings import SettingsRepository


class PgSettingsRepository(SettingsRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM user_settings WHERE key = %s", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set(self, key: str, value: str) -> None:
        self._conn.execute(
            """INSERT INTO user_settings (key, value, updated_at)
               VALUES (%s, %s, now())
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
            (key, value),
        )

    def get_all(self) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT key, value FROM user_settings ORDER BY key"
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}
```

- [ ] **Step 1c: Update RepositoryFactory**

Add to `backend/services/repository_factory.py`:

```python
from backend.db.repositories.settings import PgSettingsRepository
```

And in `__init__`:

```python
self.settings = PgSettingsRepository(conn)
```

- [ ] **Step 1d: Run repo tests**

Run: `uv run pytest tests/integration/test_pg_settings_repo.py -v`
Expected: All PASS.

- [ ] **Step 1e: Commit**

```bash
git add backend/db/repositories/settings.py backend/services/repository_factory.py tests/integration/test_pg_settings_repo.py
git commit -m "feat: PgSettingsRepository + integration tests"
```

### Step 2 — Settings router

- [ ] **Step 2a: Write settings tests**

```python
# tests/routers/test_settings.py
from __future__ import annotations

import psycopg
from psycopg.rows import dict_row


class TestGetSettings:
    def test_empty(self, client):
        resp = client.get("/api/v1/settings")
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_returns_all(self, client, db_conn):
        db_conn.execute(
            "INSERT INTO user_settings (key, value, updated_at) VALUES ('lib_root', '/music', now())"
        )
        db_conn.commit()
        resp = client.get("/api/v1/settings")
        assert resp.status_code == 200
        assert resp.json()["lib_root"] == "/music"


class TestPutSetting:
    def test_create_new(self, client):
        resp = client.put(
            "/api/v1/settings/library_root",
            json={"value": "/mnt/music"},
        )
        assert resp.status_code == 200
        assert resp.json()["key"] == "library_root"
        assert resp.json()["value"] == "/mnt/music"

    def test_overwrite(self, client, db_conn):
        db_conn.execute(
            "INSERT INTO user_settings (key, value, updated_at) VALUES ('x', 'old', now())"
        )
        db_conn.commit()
        resp = client.put("/api/v1/settings/x", json={"value": "new"})
        assert resp.status_code == 200
        assert resp.json()["value"] == "new"
```

- [ ] **Step 2b: Implement settings router**

```python
# backend/routers/settings.py
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.dependencies import get_current_token, get_db_connection

router = APIRouter()

DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
Token = Annotated[str, Depends(get_current_token)]


class SettingValue(BaseModel):
    value: str


class SettingEntry(BaseModel):
    key: str
    value: str


@router.get("")
async def get_settings(conn: DbConn, _token: Token) -> dict[str, str]:
    cur = await conn.execute(
        "SELECT key, value FROM user_settings ORDER BY key"
    )
    rows = await cur.fetchall()
    return {r["key"]: r["value"] for r in rows}


@router.put("/{key}")
async def put_setting(
    key: str, body: SettingValue, conn: DbConn, _token: Token,
) -> SettingEntry:
    await conn.execute(
        """INSERT INTO user_settings (key, value, updated_at)
           VALUES (%s, %s, now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        (key, body.value),
    )
    return SettingEntry(key=key, value=body.value)
```

- [ ] **Step 2c: Register in `v1.py`**

Add to `backend/routers/v1.py`:

```python
from backend.routers import ingestion, library, matching, playlists, settings, stations

router.include_router(settings.router, prefix="/settings", tags=["settings"])
```

- [ ] **Step 2d: Run tests**

Run: `uv run pytest tests/routers/test_settings.py -v`
Expected: All PASS.

- [ ] **Step 2e: Full suite + type check**

Run: `uv run pytest tests/ -x -q && uv run mypy --strict backend/ && uv run ruff check backend/`
Expected: All pass.

- [ ] **Step 2f: Commit**

```bash
git add backend/routers/settings.py backend/db/repositories/settings.py backend/services/repository_factory.py backend/routers/v1.py tests/routers/test_settings.py tests/integration/test_pg_settings_repo.py
git commit -m "feat: settings router — get all, put by key"
```

---

## Task 6: Active Tasks Endpoint

**Files:**
- Create: `backend/routers/tasks.py`, `tests/routers/test_tasks.py`
- Modify: `backend/routers/v1.py`

### Step 1 — Write tests

- [ ] **Step 1a: Create test file**

```python
# tests/routers/test_tasks.py
from __future__ import annotations

import json
from datetime import datetime

import psycopg


class TestActiveTasks:
    def test_empty(self, client):
        resp = client.get("/api/v1/tasks/active")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_running_tasks(self, client, db_conn):
        now = datetime.utcnow()
        db_conn.execute(
            """INSERT INTO progress_tracking
               (task_id, task_type, status, progress_data, started_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            ("task-1", "scan", "running", json.dumps({"scanned": 100}), now, now),
        )
        # Completed task should NOT appear
        db_conn.execute(
            """INSERT INTO progress_tracking
               (task_id, task_type, status, progress_data, started_at, updated_at, completed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            ("task-2", "ingestion", "completed", json.dumps({}), now, now, now),
        )
        db_conn.commit()

        resp = client.get("/api/v1/tasks/active")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["task_id"] == "task-1"
        assert data[0]["task_type"] == "scan"
        assert data[0]["progress_data"]["scanned"] == 100
```

- [ ] **Step 1b: Run tests, confirm failures**

Run: `uv run pytest tests/routers/test_tasks.py -v`
Expected: FAIL.

### Step 2 — Implement tasks router

- [ ] **Step 2a: Create `backend/routers/tasks.py`**

```python
# backend/routers/tasks.py
from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.dependencies import get_current_token, get_db_connection

router = APIRouter()

DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
Token = Annotated[str, Depends(get_current_token)]


class TaskInfo(BaseModel):
    task_id: str
    task_type: str
    status: str
    progress_data: dict[str, Any]
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@router.get("/active")
async def list_active_tasks(conn: DbConn, _token: Token) -> list[TaskInfo]:
    cur = await conn.execute(
        """SELECT * FROM progress_tracking
           WHERE status = 'running'
           ORDER BY started_at DESC"""
    )
    rows = await cur.fetchall()
    results = []
    for r in rows:
        progress = r["progress_data"]
        if isinstance(progress, str):
            progress = json.loads(progress)
        results.append(TaskInfo(
            task_id=r["task_id"],
            task_type=r["task_type"],
            status=r["status"],
            progress_data=progress,
            started_at=r["started_at"],
            updated_at=r["updated_at"],
            completed_at=r.get("completed_at"),
        ))
    return results
```

- [ ] **Step 2b: Register in `v1.py`**

Add to `backend/routers/v1.py`:

```python
from backend.routers import ingestion, library, matching, playlists, settings, stations, tasks

router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
```

- [ ] **Step 2c: Run tests**

Run: `uv run pytest tests/routers/test_tasks.py -v`
Expected: All PASS.

- [ ] **Step 2d: Full suite + type check**

Run: `uv run pytest tests/ -x -q && uv run mypy --strict backend/ && uv run ruff check backend/`
Expected: All pass.

- [ ] **Step 2e: Commit**

```bash
git add backend/routers/tasks.py backend/routers/v1.py tests/routers/test_tasks.py
git commit -m "feat: tasks router — GET /tasks/active"
```

---

## Task 7: WebSocket Progress Broadcast

**Files:**
- Create: `backend/websocket.py`, `tests/test_websocket.py`
- Modify: `backend/main.py`

### Step 1 — Write WebSocket tests

- [ ] **Step 1a: Create test file**

```python
# tests/test_websocket.py
from __future__ import annotations

import json
import os
from datetime import datetime

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row


@pytest.fixture(scope="session")
def ws_client(_migrated_db_url: str) -> TestClient:
    os.environ["DATABASE_URL"] = _migrated_db_url
    from backend.config import get_settings
    get_settings.cache_clear()
    from backend.main import app
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


class TestWebSocket:
    def test_rejects_missing_token(self, ws_client):
        with pytest.raises(Exception):
            with ws_client.websocket_connect("/ws"):
                pass

    def test_rejects_bad_token(self, ws_client):
        with pytest.raises(Exception):
            with ws_client.websocket_connect("/ws?token=wrong"):
                pass

    def test_connects_with_valid_token(self, ws_client, migrated_db):
        with ws_client.websocket_connect("/ws?token=dev-token") as ws:
            # Should receive at least one broadcast within a reasonable time
            data = ws.receive_json()
            assert "tasks" in data

    def test_broadcasts_running_tasks(self, ws_client, migrated_db):
        # Insert a running task
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            now = datetime.utcnow()
            conn.execute(
                """INSERT INTO progress_tracking
                   (task_id, task_type, status, progress_data, started_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                ("ws-task-1", "scan", "running", json.dumps({"pct": 50}), now, now),
            )
            conn.commit()

        with ws_client.websocket_connect("/ws?token=dev-token") as ws:
            data = ws.receive_json()
            assert len(data["tasks"]) >= 1
            task = next(t for t in data["tasks"] if t["task_id"] == "ws-task-1")
            assert task["progress_data"]["pct"] == 50
```

- [ ] **Step 1b: Run tests, confirm failures**

Run: `uv run pytest tests/test_websocket.py -v`
Expected: FAIL.

### Step 2 — Implement WebSocket

- [ ] **Step 2a: Create `backend/websocket.py`**

```python
# backend/websocket.py
from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from psycopg import AsyncConnection

from backend.config import get_settings
from backend.db.pool import get_pool

logger = structlog.get_logger()

POLL_INTERVAL_SECONDS = 0.5
STALE_THRESHOLD_MINUTES = 10


async def _authenticate(websocket: WebSocket) -> bool:
    """Validate token from query param."""
    token = websocket.query_params.get("token")
    settings = get_settings()
    return token == settings.airwave_token


async def _fetch_running_tasks(conn: AsyncConnection[Any]) -> list[dict[str, Any]]:
    """Fetch all running tasks from progress_tracking."""
    cur = await conn.execute(
        """SELECT task_id, task_type, status, progress_data,
                  started_at, updated_at, completed_at
           FROM progress_tracking
           WHERE status = 'running'
           ORDER BY started_at DESC"""
    )
    rows = await cur.fetchall()
    tasks = []
    for r in rows:
        progress = r["progress_data"]
        if isinstance(progress, str):
            progress = json.loads(progress)
        tasks.append({
            "task_id": r["task_id"],
            "task_type": r["task_type"],
            "status": r["status"],
            "progress_data": progress,
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        })
    return tasks


async def _mark_stale_tasks(conn: AsyncConnection[Any]) -> int:
    """Mark running tasks not updated in STALE_THRESHOLD_MINUTES as failed."""
    result = await conn.execute(
        """UPDATE progress_tracking
           SET status = 'failed'
           WHERE status = 'running'
             AND updated_at < now() - (interval '1 minute' * %s)""",
        (STALE_THRESHOLD_MINUTES,),
    )
    await conn.commit()
    return result.rowcount


async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket endpoint: polls progress_tracking every 500ms."""
    if not await _authenticate(websocket):
        await websocket.close(code=4001, reason="Invalid token")
        return

    await websocket.accept()
    pool = get_pool()

    try:
        async with pool.connection() as conn:
            while True:
                # Mark stale tasks
                stale_count = await _mark_stale_tasks(conn)
                if stale_count:
                    logger.info("websocket_stale_cleanup", count=stale_count)

                # Fetch and broadcast
                tasks = await _fetch_running_tasks(conn)
                await websocket.send_json({"tasks": tasks})

                await asyncio.sleep(POLL_INTERVAL_SECONDS)

    except WebSocketDisconnect:
        logger.debug("websocket_disconnected")
    except Exception:
        logger.exception("websocket_error")
```

- [ ] **Step 2b: Register in `main.py`**

Add to `backend/main.py` after the health endpoint:

```python
from backend.websocket import websocket_endpoint

@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket_endpoint(websocket)
```

Add the import at the top:

```python
from fastapi import FastAPI, WebSocket
```

- [ ] **Step 2c: Run tests**

Run: `uv run pytest tests/test_websocket.py -v`
Expected: All PASS.

- [ ] **Step 2d: Full suite + type check**

Run: `uv run pytest tests/ -x -q && uv run mypy --strict backend/ && uv run ruff check backend/`
Expected: All pass.

- [ ] **Step 2e: Commit**

```bash
git add backend/websocket.py backend/main.py tests/test_websocket.py
git commit -m "feat: WebSocket progress broadcast with stale task cleanup"
```

---

## Task 8: M3U Export Service + Endpoint

**Files:**
- Create: `backend/services/m3u_generator_service.py`, `tests/services/test_m3u_generator.py`
- Modify: `backend/routers/playlists.py`

### Step 1 — Write M3U generator unit tests

- [ ] **Step 1a: Create test file**

```python
# tests/services/test_m3u_generator.py
from __future__ import annotations

from uuid import uuid4

from backend.domain.enums import EnrichmentStatus, MatchStatus, MatchTier, SelectionMethod, TargetType
from backend.domain.models import (
    FormatOverride,
    LibraryFile,
    LogEvent,
    LogIdentity,
    Match,
    Recording,
    SongMaster,
    Work,
)
from backend.services.m3u_generator_service import generate_m3u
from tests.fakes.format_overrides import FakeFormatOverrideRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.log_events import FakeLogEventRepository
from tests.fakes.log_identities import FakeLogIdentityRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.recordings import FakeRecordingRepository
from tests.fakes.settings import FakeSettingsRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.works import FakeWorkRepository


def _build_chain():
    """Build a matched identity → library file chain for testing."""
    playlist_id = uuid4()
    lf_id = uuid4()
    identity_id = uuid4()

    lf = LibraryFile(
        id=lf_id,
        file_path="/music/Nirvana/01-smells.flac",
        file_hash="abc", format="flac",
        recording_id="rec-1",
        enrichment_status=EnrichmentStatus.ENRICHED,
    )
    identity = LogIdentity(
        id=identity_id, artist_id=uuid4(),
        original_title="Smells Like Teen Spirit",
        normalized_title="smells like teen spirit",
        normalized_signature="sig1",
        match_status=MatchStatus.AUTO_MATCHED,
        match_tier=MatchTier.NORMALIZATION,
    )
    event = LogEvent(
        id=uuid4(), identity_id=identity_id,
        playlist_id=playlist_id,
        played_at=__import__("datetime").datetime(2024, 1, 15, 10, 0, 0),
    )
    match = Match(
        id=uuid4(), identity_id=identity_id,
        library_file_id=lf_id,
        target_id=str(lf_id), target_type=TargetType.LIBRARY_FILE,
        confidence_score=0.95, match_tier=MatchTier.NORMALIZATION,
    )
    work = Work(id="work-1", title="Smells Like Teen Spirit", artist_id="art-1")
    recording = Recording(id="rec-1", title="Smells Like Teen Spirit", work_id="work-1")

    return playlist_id, identity, event, match, lf, work, recording


class TestGenerateM3u:
    def test_basic_export(self):
        pid, identity, event, match, lf, work, rec = _build_chain()

        events_repo = FakeLogEventRepository()
        events_repo.create(event)
        identities_repo = FakeLogIdentityRepository()
        identities_repo.upsert(identity)
        matches_repo = FakeMatchRepository()
        matches_repo.create(match)
        files_repo = FakeLibraryFileRepository()
        files_repo.upsert(lf)
        settings_repo = FakeSettingsRepository()
        masters_repo = FakeSongMasterRepository()
        overrides_repo = FakeFormatOverrideRepository()
        works_repo = FakeWorkRepository()
        works_repo.upsert(work)
        recordings_repo = FakeRecordingRepository()
        recordings_repo.upsert(rec)

        content = generate_m3u(
            playlist_id=pid,
            event_repo=events_repo,
            identity_repo=identities_repo,
            match_repo=matches_repo,
            file_repo=files_repo,
            recording_repo=recordings_repo,
            work_repo=works_repo,
            master_repo=masters_repo,
            override_repo=overrides_repo,
            settings_repo=settings_repo,
        )
        assert content.startswith("#EXTM3U")
        assert "/music/Nirvana/01-smells.flac" in content

    def test_navidrome_path_mapping(self):
        pid, identity, event, match, lf, work, rec = _build_chain()

        events_repo = FakeLogEventRepository()
        events_repo.create(event)
        identities_repo = FakeLogIdentityRepository()
        identities_repo.upsert(identity)
        matches_repo = FakeMatchRepository()
        matches_repo.create(match)
        files_repo = FakeLibraryFileRepository()
        files_repo.upsert(lf)
        settings_repo = FakeSettingsRepository(
            initial={"navidrome_path_prefix": "/data/music", "local_path_prefix": "/music"}
        )
        masters_repo = FakeSongMasterRepository()
        overrides_repo = FakeFormatOverrideRepository()
        works_repo = FakeWorkRepository()
        works_repo.upsert(work)
        recordings_repo = FakeRecordingRepository()
        recordings_repo.upsert(rec)

        content = generate_m3u(
            playlist_id=pid,
            event_repo=events_repo,
            identity_repo=identities_repo,
            match_repo=matches_repo,
            file_repo=files_repo,
            recording_repo=recordings_repo,
            work_repo=works_repo,
            master_repo=masters_repo,
            override_repo=overrides_repo,
            settings_repo=settings_repo,
        )
        assert "/data/music/Nirvana/01-smells.flac" in content

    def test_song_master_override(self):
        pid, identity, event, match, lf, work, rec = _build_chain()

        # Create a "better" file that's the song master
        better_id = uuid4()
        better_file = LibraryFile(
            id=better_id,
            file_path="/music/Nirvana/01-smells-promo.flac",
            file_hash="def", format="flac", recording_id="rec-1",
            enrichment_status=EnrichmentStatus.ENRICHED,
        )
        master = SongMaster(
            id=uuid4(), work_id="work-1", preferred_file_id=better_id,
            selection_method=SelectionMethod.MANUAL,
        )

        events_repo = FakeLogEventRepository()
        events_repo.create(event)
        identities_repo = FakeLogIdentityRepository()
        identities_repo.upsert(identity)
        matches_repo = FakeMatchRepository()
        matches_repo.create(match)
        files_repo = FakeLibraryFileRepository()
        files_repo.upsert(lf)
        files_repo.upsert(better_file)
        settings_repo = FakeSettingsRepository()
        masters_repo = FakeSongMasterRepository()
        masters_repo.upsert(master)
        overrides_repo = FakeFormatOverrideRepository()
        works_repo = FakeWorkRepository()
        works_repo.upsert(work)
        recordings_repo = FakeRecordingRepository()
        recordings_repo.upsert(rec)

        content = generate_m3u(
            playlist_id=pid,
            event_repo=events_repo,
            identity_repo=identities_repo,
            match_repo=matches_repo,
            file_repo=files_repo,
            recording_repo=recordings_repo,
            work_repo=works_repo,
            master_repo=masters_repo,
            override_repo=overrides_repo,
            settings_repo=settings_repo,
        )
        assert "/music/Nirvana/01-smells-promo.flac" in content

    def test_format_override_wins(self):
        pid, identity, event, match, lf, work, rec = _build_chain()

        # Format-specific override file
        override_id = uuid4()
        override_file = LibraryFile(
            id=override_id,
            file_path="/music/Nirvana/01-smells-radio.flac",
            file_hash="ghi", format="flac", recording_id="rec-1",
            enrichment_status=EnrichmentStatus.ENRICHED,
        )
        master = SongMaster(
            id=uuid4(), work_id="work-1", preferred_file_id=lf.id,
            selection_method=SelectionMethod.AUTO,
        )
        fo = FormatOverride(
            id=uuid4(), work_id="work-1", format_name="CHR",
            preferred_file_id=override_id,
        )

        events_repo = FakeLogEventRepository()
        events_repo.create(event)
        identities_repo = FakeLogIdentityRepository()
        identities_repo.upsert(identity)
        matches_repo = FakeMatchRepository()
        matches_repo.create(match)
        files_repo = FakeLibraryFileRepository()
        files_repo.upsert(lf)
        files_repo.upsert(override_file)
        settings_repo = FakeSettingsRepository()
        masters_repo = FakeSongMasterRepository()
        masters_repo.upsert(master)
        overrides_repo = FakeFormatOverrideRepository()
        overrides_repo.create(fo)
        works_repo = FakeWorkRepository()
        works_repo.upsert(work)
        recordings_repo = FakeRecordingRepository()
        recordings_repo.upsert(rec)

        content = generate_m3u(
            playlist_id=pid,
            event_repo=events_repo,
            identity_repo=identities_repo,
            match_repo=matches_repo,
            file_repo=files_repo,
            recording_repo=recordings_repo,
            work_repo=works_repo,
            master_repo=masters_repo,
            override_repo=overrides_repo,
            settings_repo=settings_repo,
            station_format="CHR",
        )
        assert "/music/Nirvana/01-smells-radio.flac" in content

    def test_unmatched_events_skipped(self):
        pid, identity, event, match, lf, work, rec = _build_chain()
        # Set identity to PENDING (unmatched)
        identity.match_status = MatchStatus.PENDING

        events_repo = FakeLogEventRepository()
        events_repo.create(event)
        identities_repo = FakeLogIdentityRepository()
        identities_repo.upsert(identity)
        matches_repo = FakeMatchRepository()  # No match created
        files_repo = FakeLibraryFileRepository()
        files_repo.upsert(lf)
        settings_repo = FakeSettingsRepository()
        masters_repo = FakeSongMasterRepository()
        overrides_repo = FakeFormatOverrideRepository()
        works_repo = FakeWorkRepository()
        recordings_repo = FakeRecordingRepository()

        content = generate_m3u(
            playlist_id=pid,
            event_repo=events_repo,
            identity_repo=identities_repo,
            match_repo=matches_repo,
            file_repo=files_repo,
            recording_repo=recordings_repo,
            work_repo=works_repo,
            master_repo=masters_repo,
            override_repo=overrides_repo,
            settings_repo=settings_repo,
        )
        assert content.startswith("#EXTM3U")
        # Only the header, no file entries
        lines = [l for l in content.strip().split("\n") if not l.startswith("#")]
        assert len(lines) == 0
```

- [ ] **Step 1b: Run tests, confirm failures**

Run: `uv run pytest tests/services/test_m3u_generator.py -v`
Expected: All FAIL (service not implemented).

### Step 2 — Implement M3U generator service

- [ ] **Step 2a: Create `backend/services/m3u_generator_service.py`**

```python
# backend/services/m3u_generator_service.py
"""M3U playlist generator with priority chain resolution.

Priority: format_override → song_master → direct match.
See design spec Section 3.4 for the resolution SQL.
"""
from __future__ import annotations

from uuid import UUID

import structlog

from backend.domain.enums import MatchStatus
from backend.repositories.format_overrides import FormatOverrideRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.log_events import LogEventRepository
from backend.repositories.log_identities import LogIdentityRepository
from backend.repositories.matches import MatchRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.settings import SettingsRepository
from backend.repositories.song_masters import SongMasterRepository
from backend.repositories.works import WorkRepository

logger = structlog.get_logger()

MATCHED_STATUSES = {MatchStatus.AUTO_MATCHED, MatchStatus.MAN_MATCHED}


def generate_m3u(
    *,
    playlist_id: UUID,
    event_repo: LogEventRepository,
    identity_repo: LogIdentityRepository,
    match_repo: MatchRepository,
    file_repo: LibraryFileRepository,
    recording_repo: RecordingRepository,
    work_repo: WorkRepository,
    master_repo: SongMasterRepository,
    override_repo: FormatOverrideRepository,
    settings_repo: SettingsRepository,
    station_format: str | None = None,
) -> str:
    """Generate M3U content for a playlist.

    Resolves each event to a file path using the priority chain:
    format_override → song_master → direct match.
    """
    local_prefix = settings_repo.get("local_path_prefix") or ""
    navidrome_prefix = settings_repo.get("navidrome_path_prefix") or ""

    events = event_repo.get_by_playlist(playlist_id)
    events.sort(key=lambda e: e.played_at)

    lines = ["#EXTM3U"]

    for event in events:
        identity = identity_repo.get_by_id(event.identity_id)
        if identity is None:
            continue
        if identity.match_status not in MATCHED_STATUSES:
            continue

        match = match_repo.get_by_identity(identity.id)
        if match is None or match.library_file_id is None:
            continue

        # Start with the direct match file
        resolved_file_id = match.library_file_id

        # Check if the matched file links to a recording → work
        matched_file = file_repo.get_by_id(match.library_file_id)
        if matched_file and matched_file.recording_id:
            recording = recording_repo.get_by_id(matched_file.recording_id)
            if recording and recording.work_id:
                work_id = recording.work_id

                # Priority 2: song_master overrides direct match
                master = master_repo.get_by_work(work_id)
                if master:
                    resolved_file_id = master.preferred_file_id

                # Priority 1: format_override overrides song_master
                if station_format:
                    override = override_repo.get(work_id, station_format)
                    if override:
                        resolved_file_id = override.preferred_file_id

        resolved_file = file_repo.get_by_id(resolved_file_id)
        if resolved_file is None:
            logger.warning(
                "m3u_file_not_found", file_id=str(resolved_file_id),
                identity_id=str(identity.id),
            )
            continue

        file_path = resolved_file.file_path

        # Apply Navidrome path mapping
        if local_prefix and navidrome_prefix and file_path.startswith(local_prefix):
            file_path = navidrome_prefix + file_path[len(local_prefix):]

        duration_secs = (resolved_file.duration_ms // 1000) if resolved_file.duration_ms else -1
        title = identity.original_title
        lines.append(f"#EXTINF:{duration_secs},{title}")
        lines.append(file_path)

    return "\n".join(lines) + "\n"
```

- [ ] **Step 2b: Run service tests**

Run: `uv run pytest tests/services/test_m3u_generator.py -v`
Expected: All PASS.

- [ ] **Step 2c: Commit service**

```bash
git add backend/services/m3u_generator_service.py tests/services/test_m3u_generator.py
git commit -m "feat: M3U generator service with priority chain resolution"
```

### Step 3 — Export endpoint

- [ ] **Step 3a: Add export endpoint to playlists router**

Add to `backend/routers/playlists.py`:

```python
# Add these imports at top:
import asyncio

import psycopg
from psycopg.rows import dict_row
from fastapi.responses import Response

from backend.config import get_settings
from backend.services.m3u_generator_service import generate_m3u as _generate_m3u
from backend.services.repository_factory import RepositoryFactory


# Add schema:
class ExportM3uBody(BaseModel):
    station_format: str | None = None


# Add endpoint:
@router.post("/{playlist_id}/export-m3u")
async def export_m3u(
    playlist_id: UUID,
    conn: DbConn,
    _token: Token,
    body: ExportM3uBody | None = None,
) -> Response:
    # Verify playlist exists
    cur = await conn.execute(
        "SELECT * FROM playlists WHERE id = %s", (playlist_id,)
    )
    playlist = await cur.fetchone()
    if playlist is None:
        raise HTTPException(status_code=404, detail="Playlist not found")

    station_format = body.station_format if body else None

    # Run M3U generation in thread with sync repos
    settings = get_settings()
    content = await asyncio.to_thread(
        _generate_m3u_sync, str(playlist_id), settings.database_url, station_format,
    )

    filename = f"{playlist['name'].replace('.csv', '')}.m3u"
    return Response(
        content=content,
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _generate_m3u_sync(
    playlist_id_str: str, database_url: str, station_format: str | None,
) -> str:
    from uuid import UUID as _UUID
    pid = _UUID(playlist_id_str)
    with psycopg.connect(database_url, row_factory=dict_row) as sync_conn:
        repos = RepositoryFactory(sync_conn)
        return _generate_m3u(
            playlist_id=pid,
            event_repo=repos.log_events,
            identity_repo=repos.log_identities,
            match_repo=repos.matches,
            file_repo=repos.library_files,
            recording_repo=repos.recordings,
            work_repo=repos.works,
            master_repo=repos.song_masters,
            override_repo=repos.format_overrides,
            settings_repo=repos.settings,
            station_format=station_format,
        )
```

- [ ] **Step 3b: Write endpoint test**

Add to `tests/routers/test_playlists.py`:

```python
class TestExportM3u:
    def test_empty_playlist(self, client, db_conn):
        station = _seed_station(db_conn)
        playlist = _seed_playlist(db_conn, station.id)
        resp = client.post(f"/api/v1/playlists/{playlist.id}/export-m3u")
        assert resp.status_code == 200
        assert resp.text.startswith("#EXTM3U")

    def test_not_found(self, client):
        resp = client.post(f"/api/v1/playlists/{uuid4()}/export-m3u")
        assert resp.status_code == 404
```

- [ ] **Step 3c: Run all tests**

Run: `uv run pytest tests/ -x -q && uv run mypy --strict backend/ && uv run ruff check backend/`
Expected: All pass.

- [ ] **Step 3d: Commit**

```bash
git add backend/routers/playlists.py tests/routers/test_playlists.py
git commit -m "feat: POST /playlists/{id}/export-m3u endpoint with Navidrome path mapping"
```

---

## Final Gate: Full Verification

- [ ] **Run complete test suite**

```bash
uv run pytest tests/ -v --tb=short
```

Expected: All tests pass (231 existing + new router/service tests).

- [ ] **Type check**

```bash
uv run mypy --strict backend/
```

Expected: Zero errors.

- [ ] **Lint**

```bash
uv run ruff check backend/
```

Expected: Zero warnings.

- [ ] **Verify all endpoints return real data**

Start the server and verify each endpoint category:

```bash
# Start in separate terminal:
# uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000

# Stations
curl -s -H "X-Airwave-Token: dev-token" http://localhost:8000/api/v1/stations | python -m json.tool

# Settings
curl -s -H "X-Airwave-Token: dev-token" http://localhost:8000/api/v1/settings | python -m json.tool

# Tasks
curl -s -H "X-Airwave-Token: dev-token" http://localhost:8000/api/v1/tasks/active | python -m json.tool

# Library status
curl -s -H "X-Airwave-Token: dev-token" http://localhost:8000/api/v1/library/status | python -m json.tool

# Health
curl -s http://localhost:8000/health
```

All should return JSON without errors.

**Phase 3 gate passed when:** All API endpoints return real data from the running backend.

---

## Final `v1.py` Reference

After all tasks complete, `backend/routers/v1.py` should contain:

```python
from __future__ import annotations

from fastapi import APIRouter

from backend.routers import (
    ingestion,
    library,
    matching,
    playlists,
    settings,
    stations,
    tasks,
)

router = APIRouter(prefix="/api/v1")
router.include_router(stations.router, prefix="/stations", tags=["stations"])
router.include_router(ingestion.router, prefix="/ingestion", tags=["ingestion"])
router.include_router(library.router, prefix="/library", tags=["library"])
router.include_router(playlists.router, prefix="/playlists", tags=["playlists"])
router.include_router(matching.router, prefix="/matching", tags=["matching"])
router.include_router(settings.router, prefix="/settings", tags=["settings"])
router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
```

---

## Note: Zod Schema Stubs

The design spec says "Each router session also fleshes out the corresponding Zod schema stubs from Phase 0." No frontend directory exists yet — **Zod schemas will be created in Phase 4** alongside each frontend page, written against the live API shapes from this phase.

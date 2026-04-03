# Calendar Zoom Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the month-only station calendar with an in-place year→month→day zoom picker scoped to station-level broadcast data, with cross-playlist event filtering.

**Architecture:** Backend-first — add repository method + generalize M3U service + add 3 new station endpoints, then frontend — new Zod schemas + query hooks, refactored DatePicker with zoom levels, new StationEventTable, simplified PlaylistViewer, and uploaded-playlists section on StationDashboard.

**Tech Stack:** Python/FastAPI/psycopg (backend), React 19/TypeScript/TanStack Query/Tailwind 4 (frontend)

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `backend/repositories/log_events.py` | Add `get_by_station_date` abstract method |
| Modify | `backend/db/repositories/log_events.py` | Implement `get_by_station_date` in `PgLogEventRepository` |
| Modify | `tests/fakes/log_events.py` | Add `get_by_station_date` to `FakeLogEventRepository` |
| Modify | `backend/services/m3u_generator_service.py` | Change `generate_m3u` to accept `events` list instead of `playlist_id` |
| Modify | `backend/routers/playlists.py` | Update `_generate_m3u_sync` to fetch events before calling `generate_m3u` |
| Modify | `tests/services/test_m3u_generator.py` | Update all tests for new `generate_m3u` signature |
| Modify | `backend/routers/stations.py` | Add 3 new endpoints + Pydantic models |
| Modify | `tests/routers/test_stations.py` | Add tests for new endpoints |
| Modify | `frontend/src/lib/schemas/stations.ts` | Add `StationEventItem` + `StationPaginatedEvents` Zod schemas |
| Modify | `frontend/src/api/stations.ts` | Add `useStationBroadcastDays`, `useStationEvents`, `useExportStationM3u` hooks |
| Modify | `frontend/src/components/domain/playlists/DatePicker.tsx` | Refactor to year→month→day zoom levels |
| Create | `frontend/src/components/domain/stations/StationEventTable.tsx` | New component (replaces PlaylistEventTable for station+date) |
| Modify | `frontend/src/pages/stations/PlaylistViewer.tsx` | Simplify: remove playlist list, use station-level hooks |
| Modify | `frontend/src/pages/stations/StationDashboard.tsx` | Add uploaded playlists section |

---

### Task 1: Add `get_by_station_date` to LogEventRepository

**Files:**
- Modify: `backend/repositories/log_events.py:1-18`
- Modify: `backend/db/repositories/log_events.py:42-54`
- Modify: `tests/fakes/log_events.py:1-27`

- [ ] **Step 1: Add abstract method to `LogEventRepository`**

In `backend/repositories/log_events.py`, add the import and new abstract method:

```python
from abc import ABC, abstractmethod
from datetime import date
from uuid import UUID

from backend.domain.models import LogEvent


class LogEventRepository(ABC):
    @abstractmethod
    def create(self, event: LogEvent) -> LogEvent:
        """Insert or ignore on (identity_id, playlist_id, played_at) conflict."""
        ...

    @abstractmethod
    def get_by_playlist(self, playlist_id: UUID) -> list[LogEvent]: ...

    @abstractmethod
    def get_by_identity(self, identity_id: UUID) -> list[LogEvent]: ...

    @abstractmethod
    def get_by_station_date(self, station_id: UUID, broadcast_date: date) -> list[LogEvent]: ...
```

- [ ] **Step 2: Implement in `PgLogEventRepository`**

In `backend/db/repositories/log_events.py`, add at the end of the class (after line 54):

```python
    def get_by_station_date(self, station_id: UUID, broadcast_date: date) -> list[LogEvent]:
        rows = self._conn.execute(
            """SELECT le.* FROM log_events le
               JOIN playlists p ON p.id = le.playlist_id
               WHERE p.station_id = %s AND le.played_at::date = %s
               ORDER BY le.played_at""",
            (station_id, broadcast_date),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]
```

Add the `date` import at the top — change line 1-4 to:

```python
from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID
```

- [ ] **Step 3: Implement in `FakeLogEventRepository`**

In `tests/fakes/log_events.py`, add the `date` import and method. The full file becomes:

```python
from datetime import date
from uuid import UUID

from backend.domain.models import LogEvent
from backend.repositories.log_events import LogEventRepository


class FakeLogEventRepository(LogEventRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, LogEvent] = {}
        self._playlist_station_map: dict[UUID, UUID] = {}

    def set_playlist_station(self, playlist_id: UUID, station_id: UUID) -> None:
        """Test helper: register which station a playlist belongs to."""
        self._playlist_station_map[playlist_id] = station_id

    def create(self, event: LogEvent) -> LogEvent:
        key = (event.identity_id, event.playlist_id, event.played_at)
        existing = next(
            (e for e in self._data.values()
             if (e.identity_id, e.playlist_id, e.played_at) == key), None
        )
        if existing:
            return existing
        self._data[event.id] = event
        return event

    def get_by_playlist(self, playlist_id: UUID) -> list[LogEvent]:
        return [e for e in self._data.values() if e.playlist_id == playlist_id]

    def get_by_identity(self, identity_id: UUID) -> list[LogEvent]:
        return [e for e in self._data.values() if e.identity_id == identity_id]

    def get_by_station_date(self, station_id: UUID, broadcast_date: date) -> list[LogEvent]:
        playlist_ids = {
            pid for pid, sid in self._playlist_station_map.items()
            if sid == station_id
        }
        return [
            e for e in self._data.values()
            if e.playlist_id in playlist_ids and e.played_at.date() == broadcast_date
        ]
```

- [ ] **Step 4: Verify the fakes-implement-ABCs test still passes**

Run: `python -m pytest tests/test_fakes_implement_abcs.py -v`
Expected: PASS (all fakes implement their ABCs correctly)

- [ ] **Step 5: Commit**

```bash
git add backend/repositories/log_events.py backend/db/repositories/log_events.py tests/fakes/log_events.py
git commit -m "feat(repo): add get_by_station_date to LogEventRepository"
```

---

### Task 2: Generalize `generate_m3u` service signature

**Files:**
- Modify: `backend/services/m3u_generator_service.py:31-71`
- Modify: `tests/services/test_m3u_generator.py:111-136`

- [ ] **Step 1: Update `generate_m3u` to accept `events` parameter**

Replace the full function in `backend/services/m3u_generator_service.py` (lines 31–122):

```python
def generate_m3u(
    *,
    events: list[LogEvent],
    identity_repo: LogIdentityRepository,
    match_repo: MatchRepository,
    file_repo: LibraryFileRepository,
    recording_repo: RecordingRepository,
    master_repo: SongMasterRepository,
    override_repo: FormatOverrideRepository,
    settings_repo: SettingsRepository,
    station_format: str | None = None,
) -> str:
    """Generate an M3U playlist string for the given events.

    Args:
        events: Pre-fetched list of log events to export.
        identity_repo: Repository for log identities.
        match_repo: Repository for identity matches.
        file_repo: Repository for library files.
        recording_repo: Repository for MusicBrainz recordings.
        master_repo: Repository for song masters.
        override_repo: Repository for format overrides.
        settings_repo: Repository for user settings.
        station_format: Optional station format string used for format_override
            lookup (e.g. ``"CHR"``).

    Returns:
        A UTF-8 M3U string beginning with ``#EXTM3U``.
    """
    local_prefix: str = settings_repo.get("local_path_prefix") or ""
    navidrome_prefix: str = settings_repo.get("navidrome_path_prefix") or ""

    sorted_events = sorted(events, key=lambda e: e.played_at)

    lines: list[str] = ["#EXTM3U"]

    for event in sorted_events:
        identity = identity_repo.get_by_id(event.identity_id)
        if identity is None or identity.match_status not in _MATCHED_STATUSES:
            continue

        match = match_repo.get_by_identity(identity.id)
        if match is None or match.library_file_id is None:
            continue

        resolved_file_id: UUID = match.library_file_id

        # Attempt to walk up to a work so we can check master / format override.
        direct_file = file_repo.get_by_id(match.library_file_id)
        if direct_file is not None and direct_file.recording_id is not None:
            recording = recording_repo.get_by_id(direct_file.recording_id)
            if recording is not None and recording.work_id is not None:
                work_id: str = recording.work_id

                # Priority 1 (lowest): song_master
                master = master_repo.get_by_work(work_id)
                if master is not None:
                    resolved_file_id = master.preferred_file_id

                # Priority 2 (highest): format_override
                if station_format is not None:
                    override = override_repo.get(work_id, station_format)
                    if override is not None:
                        resolved_file_id = override.preferred_file_id

        resolved_file = file_repo.get_by_id(resolved_file_id)
        if resolved_file is None:
            continue

        file_path = resolved_file.file_path
        if local_prefix and navidrome_prefix and file_path.startswith(local_prefix):
            file_path = navidrome_prefix + file_path[len(local_prefix):]

        duration_secs: int = (
            resolved_file.duration_ms // 1000
            if resolved_file.duration_ms is not None
            else -1
        )
        title: str = identity.original_title

        lines.append(f"#EXTINF:{duration_secs},{title}")
        lines.append(file_path)

    return "\n".join(lines) + "\n"
```

Also update the imports at the top of the file — add `LogEvent` import. The imports section (lines 14-24) becomes:

```python
from backend.domain.enums import MatchStatus
from backend.domain.models import LogEvent
from backend.repositories.format_overrides import FormatOverrideRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.log_identities import LogIdentityRepository
from backend.repositories.matches import MatchRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.settings import SettingsRepository
from backend.repositories.song_masters import SongMasterRepository
```

Remove the `LogEventRepository` import (no longer needed).

- [ ] **Step 2: Update `_call_generate` helper in test file**

In `tests/services/test_m3u_generator.py`, replace the `_call_generate` function (lines 111-136):

```python
def _call_generate(
    playlist_id: object,
    *,
    event_repo: FakeLogEventRepository,
    identity_repo: FakeLogIdentityRepository,
    match_repo: FakeMatchRepository,
    file_repo: FakeLibraryFileRepository,
    recording_repo: FakeRecordingRepository,
    master_repo: FakeSongMasterRepository,
    override_repo: FakeFormatOverrideRepository,
    settings_repo: FakeSettingsRepository,
    station_format: str | None = None,
) -> str:
    from uuid import UUID
    events = event_repo.get_by_playlist(UUID(str(playlist_id)))
    return generate_m3u(
        events=events,
        identity_repo=identity_repo,
        match_repo=match_repo,
        file_repo=file_repo,
        recording_repo=recording_repo,
        master_repo=master_repo,
        override_repo=override_repo,
        settings_repo=settings_repo,
        station_format=station_format,
    )
```

- [ ] **Step 3: Run the M3U generator tests**

Run: `python -m pytest tests/services/test_m3u_generator.py -v`
Expected: All 6 tests PASS

- [ ] **Step 4: Commit**

```bash
git add backend/services/m3u_generator_service.py tests/services/test_m3u_generator.py
git commit -m "refactor(m3u): generalize generate_m3u to accept events list"
```

---

### Task 3: Update `_generate_m3u_sync` in playlists router

**Files:**
- Modify: `backend/routers/playlists.py:299-328`

- [ ] **Step 1: Update `_generate_m3u_sync` to fetch events first**

In `backend/routers/playlists.py`, replace lines 299-328:

```python
def _generate_m3u_sync(
    playlist_id_str: str,
    database_url: str,
    station_format: str | None,
) -> str:
    """Run M3U generation on a sync psycopg connection (for use with to_thread).

    Args:
        playlist_id_str: String representation of the playlist UUID.
        database_url: PostgreSQL connection string.
        station_format: Optional station format for format_override lookup.

    Returns:
        The M3U text.
    """
    pid = UUID(playlist_id_str)
    with psycopg.connect(database_url, row_factory=dict_row) as sync_conn:
        repos = RepositoryFactory(sync_conn)
        events = repos.log_events.get_by_playlist(pid)
        return generate_m3u(
            events=events,
            identity_repo=repos.log_identities,
            match_repo=repos.matches,
            file_repo=repos.library_files,
            recording_repo=repos.recordings,
            master_repo=repos.song_masters,
            override_repo=repos.format_overrides,
            settings_repo=repos.settings,
            station_format=station_format,
        )
```

- [ ] **Step 2: Run the playlist export router test**

Run: `python -m pytest tests/routers/test_playlists.py::TestExportM3u -v`
Expected: Both tests PASS

- [ ] **Step 3: Commit**

```bash
git add backend/routers/playlists.py
git commit -m "fix(playlists): update _generate_m3u_sync for new generate_m3u signature"
```

---

### Task 4: Add 3 new station endpoints

**Files:**
- Modify: `backend/routers/stations.py:1-206`
- Modify: `tests/routers/test_stations.py`

- [ ] **Step 1: Write tests for the new endpoints**

Append to `tests/routers/test_stations.py`:

```python
from datetime import date, datetime, timezone

from backend.db.repositories.broadcast_days import PgBroadcastDayRepository
from backend.db.repositories.log_artists import PgLogArtistRepository
from backend.db.repositories.log_events import PgLogEventRepository
from backend.db.repositories.log_identities import PgLogIdentityRepository
from backend.db.repositories.playlists import PgPlaylistRepository
from backend.domain.enums import MatchStatus
from backend.domain.models import (
    LogArtist,
    LogEvent,
    LogIdentity,
    Playlist,
)


def _insert_playlist(conn, station, name="show.csv"):
    playlist = Playlist(
        id=uuid4(), name=name, content_hash=uuid4().hex, station_id=station.id,
    )
    result = PgPlaylistRepository(conn).create(playlist)
    conn.commit()
    return result


def _insert_event_full(conn, playlist, artist_name="Test Artist", title="Test Song", played_at=None):
    """Insert artist + identity + event. Returns the event."""
    artist = LogArtist(
        id=uuid4(), original_name=artist_name,
        normalized_name=artist_name.lower(), match_status=MatchStatus.PENDING,
    )
    PgLogArtistRepository(conn).upsert(artist)

    identity = LogIdentity(
        id=uuid4(), artist_id=artist.id, original_title=title,
        normalized_title=title.lower(),
        normalized_signature=f"{artist_name.lower()}:{title.lower()}",
        match_status=MatchStatus.PENDING,
    )
    PgLogIdentityRepository(conn).upsert(identity)

    event = LogEvent(
        id=uuid4(), identity_id=identity.id, playlist_id=playlist.id,
        played_at=played_at or datetime.now(tz=timezone.utc),
    )
    PgLogEventRepository(conn).create(event)
    conn.commit()
    return event


class TestStationBroadcastDays:
    def test_returns_dates(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        repo = PgBroadcastDayRepository(db_conn)
        repo.get_or_create(station.id, date(2001, 3, 15))
        repo.get_or_create(station.id, date(2001, 6, 20))
        db_conn.commit()

        resp = client.get(f"/api/v1/stations/{station.id}/broadcast-days")
        assert resp.status_code == 200
        assert resp.json() == ["2001-03-15", "2001-06-20"]

    def test_empty(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        resp = client.get(f"/api/v1/stations/{station.id}/broadcast-days")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_not_found(self, client):
        resp = client.get(f"/api/v1/stations/{uuid4()}/broadcast-days")
        assert resp.status_code == 404


class TestStationEventsByDate:
    def test_returns_events(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station, name="morning.csv")
        _insert_event_full(
            db_conn, playlist, "The Clash", "London Calling",
            played_at=datetime(2001, 3, 15, 8, 0, 0, tzinfo=timezone.utc),
        )
        _insert_event_full(
            db_conn, playlist, "Nirvana", "Smells Like Teen Spirit",
            played_at=datetime(2001, 3, 15, 9, 0, 0, tzinfo=timezone.utc),
        )

        resp = client.get(f"/api/v1/stations/{station.id}/events?date=2001-03-15")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        assert data["items"][0]["artist_name"] == "The Clash"
        assert data["items"][0]["playlist_name"] == "morning.csv"

    def test_cross_playlist(self, client, db_conn):
        """Events from multiple playlists on the same date are returned."""
        station = _insert_station(db_conn, "KAZR-FM")
        p1 = _insert_playlist(db_conn, station, name="morning.csv")
        p2 = _insert_playlist(db_conn, station, name="evening.csv")
        _insert_event_full(
            db_conn, p1, "The Clash", "London Calling",
            played_at=datetime(2001, 3, 15, 8, 0, 0, tzinfo=timezone.utc),
        )
        _insert_event_full(
            db_conn, p2, "Nirvana", "In Bloom",
            played_at=datetime(2001, 3, 15, 20, 0, 0, tzinfo=timezone.utc),
        )

        resp = client.get(f"/api/v1/stations/{station.id}/events?date=2001-03-15")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        names = {item["playlist_name"] for item in data["items"]}
        assert names == {"morning.csv", "evening.csv"}

    def test_excludes_other_dates(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        _insert_event_full(
            db_conn, playlist, "The Clash", "London Calling",
            played_at=datetime(2001, 3, 15, 8, 0, 0, tzinfo=timezone.utc),
        )
        _insert_event_full(
            db_conn, playlist, "Nirvana", "In Bloom",
            played_at=datetime(2001, 3, 16, 8, 0, 0, tzinfo=timezone.utc),
        )

        resp = client.get(f"/api/v1/stations/{station.id}/events?date=2001-03-15")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_pagination(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        for i in range(5):
            _insert_event_full(
                db_conn, playlist, f"Artist {i}", f"Song {i}",
                played_at=datetime(2001, 3, 15, 8, i, 0, tzinfo=timezone.utc),
            )

        resp = client.get(f"/api/v1/stations/{station.id}/events?date=2001-03-15&limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2

    def test_not_found(self, client):
        resp = client.get(f"/api/v1/stations/{uuid4()}/events?date=2001-03-15")
        assert resp.status_code == 404


class TestStationExportM3u:
    def test_export_empty_date(self, client, db_conn):
        """A station with no events on that date returns a valid M3U header."""
        station = _insert_station(db_conn, "KAZR-FM")
        resp = client.post(
            f"/api/v1/stations/{station.id}/export-m3u",
            json={"date": "2001-03-15"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("audio/x-mpegurl")
        assert "Content-Disposition" in resp.headers
        assert "KAZR-FM-2001-03-15.m3u" in resp.headers["Content-Disposition"]
        assert resp.text.startswith("#EXTM3U")

    def test_not_found(self, client):
        resp = client.post(
            f"/api/v1/stations/{uuid4()}/export-m3u",
            json={"date": "2001-03-15"},
        )
        assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/routers/test_stations.py::TestStationBroadcastDays tests/routers/test_stations.py::TestStationEventsByDate tests/routers/test_stations.py::TestStationExportM3u -v`
Expected: FAIL (endpoints don't exist yet)

- [ ] **Step 3: Add Pydantic models and endpoints to `stations.py`**

In `backend/routers/stations.py`, add these imports at the top (merge with existing):

```python
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
```

Add these Pydantic models after the existing `StationSummary` class (after line 51):

```python
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
```

Add the helper to verify station existence (add after `_row_to_response` helper, before the routes section):

```python
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
```

Add the three new route handlers at the end of the file (after the `delete_station` handler):

```python
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
        FROM log_events le
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
        FROM log_events le
        JOIN log_identities li ON li.id = le.identity_id
        JOIN log_artists la ON la.id = li.artist_id
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
        events = repos.log_events.get_by_station_date(sid, d)
        return generate_m3u(
            events=events,
            identity_repo=repos.log_identities,
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
```

- [ ] **Step 4: Run all new station endpoint tests**

Run: `python -m pytest tests/routers/test_stations.py -v`
Expected: All tests PASS (existing + new)

- [ ] **Step 5: Commit**

```bash
git add backend/routers/stations.py tests/routers/test_stations.py
git commit -m "feat(stations): add broadcast-days, events-by-date, and export-m3u endpoints"
```

---

### Task 5: Add Zod schemas and query hooks

**Files:**
- Modify: `frontend/src/lib/schemas/stations.ts:1-44`
- Modify: `frontend/src/api/stations.ts:1-75`

- [ ] **Step 1: Add Zod schemas to `stations.ts`**

Append to `frontend/src/lib/schemas/stations.ts` (after line 43):

```typescript
// ---------------------------------------------------------------------------
// Station event shapes (calendar zoom feature)
// ---------------------------------------------------------------------------

export const StationEventItemSchema = z.object({
  id: z.string().uuid(),
  played_at: z.string(),
  artist_name: z.string(),
  title: z.string(),
  match_status: z.string(),
  match_tier: z.string().nullable(),
  playlist_name: z.string(),
});
export type StationEventItem = z.infer<typeof StationEventItemSchema>;

export const StationPaginatedEventsSchema = z.object({
  items: z.array(StationEventItemSchema),
  total: z.number(),
});
export type StationPaginatedEvents = z.infer<typeof StationPaginatedEventsSchema>;
```

- [ ] **Step 2: Add query hooks and export mutation to `api/stations.ts`**

Replace the full file `frontend/src/api/stations.ts`:

```typescript
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiFetch, apiDownload } from "@/api/client";
import type {
  StationList,
  StationResponse,
  StationCreate,
  StationUpdate,
  StationPaginatedEvents,
} from "@/lib/schemas/stations";

const STATIONS_KEY = ["stations"] as const;
const stationKey = (id: string) => ["stations", id] as const;
const stationBroadcastDaysKey = (stationId: string) =>
  ["stations", stationId, "broadcast-days"] as const;
const stationEventsKey = (
  stationId: string,
  date: string,
  limit: number,
  offset: number,
) => ["stations", stationId, "events", { date, limit, offset }] as const;

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

export function useStations() {
  return useQuery<StationList>({
    queryKey: STATIONS_KEY,
    queryFn: () => apiFetch<StationList>("/api/v1/stations"),
  });
}

export function useStation(id: string | undefined) {
  return useQuery<StationResponse>({
    queryKey: stationKey(id ?? ""),
    queryFn: () => apiFetch<StationResponse>(`/api/v1/stations/${id}`),
    enabled: Boolean(id),
  });
}

export function useStationBroadcastDays(stationId: string | undefined) {
  return useQuery<string[]>({
    queryKey: stationBroadcastDaysKey(stationId ?? ""),
    queryFn: () =>
      apiFetch<string[]>(`/api/v1/stations/${stationId}/broadcast-days`),
    enabled: Boolean(stationId),
  });
}

export function useStationEvents(
  stationId: string | undefined,
  date: string | undefined,
  limit: number,
  offset: number,
) {
  return useQuery<StationPaginatedEvents>({
    queryKey: stationEventsKey(stationId ?? "", date ?? "", limit, offset),
    queryFn: () =>
      apiFetch<StationPaginatedEvents>(
        `/api/v1/stations/${stationId}/events?date=${date}&limit=${limit}&offset=${offset}`,
      ),
    enabled: Boolean(stationId) && Boolean(date),
  });
}

// ---------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------

export function useCreateStation() {
  const qc = useQueryClient();
  return useMutation<StationResponse, Error, StationCreate>({
    mutationFn: (payload) =>
      apiFetch<StationResponse>("/api/v1/stations", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: STATIONS_KEY });
    },
  });
}

export function useUpdateStation(id: string) {
  const qc = useQueryClient();
  return useMutation<StationResponse, Error, StationUpdate>({
    mutationFn: (payload) =>
      apiFetch<StationResponse>(`/api/v1/stations/${id}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: STATIONS_KEY });
      void qc.invalidateQueries({ queryKey: stationKey(id) });
    },
  });
}

export function useDeleteStation() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) =>
      apiFetch<void>(`/api/v1/stations/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: STATIONS_KEY });
    },
  });
}

interface ExportStationM3uVariables {
  stationId: string;
  date: string;
  callLetters: string;
}

export function useExportStationM3u() {
  return useMutation<void, Error, ExportStationM3uVariables>({
    mutationFn: async ({ stationId, date, callLetters }) => {
      const blob = await apiDownload(
        `/api/v1/stations/${stationId}/export-m3u`,
        { date },
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${callLetters}-${date}.m3u`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    },
  });
}
```

- [ ] **Step 3: Verify TypeScript compilation**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/schemas/stations.ts frontend/src/api/stations.ts
git commit -m "feat(frontend): add station event schemas and query hooks"
```

---

### Task 6: Refactor DatePicker with zoom levels

**Files:**
- Modify: `frontend/src/components/domain/playlists/DatePicker.tsx:1-155`

- [ ] **Step 1: Rewrite DatePicker with year/month/day views**

Replace the full file `frontend/src/components/domain/playlists/DatePicker.tsx`:

```tsx
import { useMemo, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type View = "years" | "months" | "days";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function getDaysInMonth(year: number, month: number): Date[] {
  const days: Date[] = [];
  const date = new Date(year, month, 1);
  while (date.getMonth() === month) {
    days.push(new Date(date));
    date.setDate(date.getDate() + 1);
  }
  return days;
}

function startDayOfWeek(year: number, month: number): number {
  return new Date(year, month, 1).getDay(); // 0 = Sunday
}

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const MONTH_ABBRS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

const DAY_LABELS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface DatePickerProps {
  broadcastDays: string[];
  selectedDate: string | undefined;
  onSelect: (date: string) => void;
  month: Date;
  onMonthChange: (month: Date) => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function DatePicker({
  broadcastDays,
  selectedDate,
  onSelect,
  month,
  onMonthChange,
}: DatePickerProps) {
  const [view, setView] = useState<View>("years");
  const [selectedYear, setSelectedYear] = useState<number | undefined>(
    undefined,
  );

  // Derived data from broadcastDays
  const { availableYears, monthsByYear, broadcastSet } = useMemo(() => {
    const set = new Set(broadcastDays);
    const yearSet = new Set<number>();
    const mByY = new Map<number, Set<number>>();

    for (const iso of broadcastDays) {
      const y = parseInt(iso.slice(0, 4), 10);
      const m = parseInt(iso.slice(5, 7), 10) - 1; // 0-indexed
      yearSet.add(y);
      if (!mByY.has(y)) mByY.set(y, new Set());
      mByY.get(y)!.add(m);
    }

    return {
      availableYears: Array.from(yearSet).sort((a, b) => a - b),
      monthsByYear: mByY,
      broadcastSet: set,
    };
  }, [broadcastDays]);

  // --- Year Grid ---
  if (view === "years") {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
        <div className="mb-3 text-center">
          <span className="text-sm font-semibold text-gray-800">
            Select Year
          </span>
        </div>
        {availableYears.length === 0 ? (
          <p className="text-center text-xs text-gray-400">
            No broadcast data available
          </p>
        ) : (
          <div className="grid grid-cols-3 gap-2">
            {availableYears.map((y) => (
              <button
                key={y}
                type="button"
                onClick={() => {
                  setSelectedYear(y);
                  setView("months");
                }}
                className="rounded-lg bg-indigo-50 px-2 py-2 text-sm font-semibold text-indigo-700 transition hover:bg-indigo-100"
              >
                {y}
              </button>
            ))}
          </div>
        )}
      </div>
    );
  }

  // --- Month Grid ---
  if (view === "months" && selectedYear !== undefined) {
    const activeMonths = monthsByYear.get(selectedYear) ?? new Set<number>();
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
        <div className="mb-3 text-center">
          <button
            type="button"
            onClick={() => {
              setView("years");
              setSelectedYear(undefined);
            }}
            className="text-sm font-semibold text-indigo-600 underline decoration-dotted underline-offset-2 hover:text-indigo-800"
          >
            {selectedYear}
          </button>
        </div>
        <div className="grid grid-cols-3 gap-2">
          {MONTH_ABBRS.map((abbr, i) => {
            const hasData = activeMonths.has(i);
            return (
              <button
                key={abbr}
                type="button"
                disabled={!hasData}
                onClick={() => {
                  onMonthChange(new Date(selectedYear, i, 1));
                  setView("days");
                }}
                className={cn(
                  "rounded-lg px-2 py-2 text-sm transition",
                  hasData &&
                    "bg-blue-50 font-medium text-blue-700 hover:bg-blue-100 cursor-pointer",
                  !hasData && "text-gray-300 cursor-not-allowed",
                )}
              >
                {abbr}
              </button>
            );
          })}
        </div>
      </div>
    );
  }

  // --- Day Grid ---
  const year = month.getFullYear();
  const monthIndex = month.getMonth();
  const days = getDaysInMonth(year, monthIndex);
  const leadingBlanks = startDayOfWeek(year, monthIndex);

  const goToPrev = () => {
    const d = new Date(year, monthIndex - 1, 1);
    onMonthChange(d);
    setSelectedYear(d.getFullYear());
  };

  const goToNext = () => {
    const d = new Date(year, monthIndex + 1, 1);
    onMonthChange(d);
    setSelectedYear(d.getFullYear());
  };

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      {/* Month navigation */}
      <div className="mb-3 flex items-center justify-between">
        <button
          type="button"
          onClick={goToPrev}
          className="rounded p-1 text-gray-500 hover:bg-gray-100 hover:text-gray-700"
          aria-label="Previous month"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <button
          type="button"
          onClick={() => {
            setView("months");
            setSelectedYear(year);
          }}
          className="text-sm font-semibold text-indigo-600 underline decoration-dotted underline-offset-2 hover:text-indigo-800"
        >
          {MONTH_NAMES[monthIndex]} {year}
        </button>
        <button
          type="button"
          onClick={goToNext}
          className="rounded p-1 text-gray-500 hover:bg-gray-100 hover:text-gray-700"
          aria-label="Next month"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>

      {/* Day-of-week headers */}
      <div className="mb-1 grid grid-cols-7 text-center">
        {DAY_LABELS.map((label) => (
          <span key={label} className="text-xs font-medium text-gray-400">
            {label}
          </span>
        ))}
      </div>

      {/* Calendar grid */}
      <div className="grid grid-cols-7 gap-y-0.5">
        {Array.from({ length: leadingBlanks }).map((_, i) => (
          <span key={`blank-${i}`} />
        ))}

        {days.map((day) => {
          const iso = isoDate(day);
          const hasBroadcast = broadcastSet.has(iso);
          const isSelected = iso === selectedDate;

          return (
            <button
              key={iso}
              type="button"
              onClick={() => hasBroadcast && onSelect(iso)}
              disabled={!hasBroadcast}
              aria-label={`${iso}${hasBroadcast ? " — has broadcasts" : ""}`}
              aria-pressed={isSelected}
              className={cn(
                "mx-auto flex h-7 w-7 items-center justify-center rounded-full text-xs transition",
                isSelected &&
                  "bg-indigo-600 font-semibold text-white",
                !isSelected &&
                  hasBroadcast &&
                  "bg-blue-100 font-medium text-blue-700 hover:bg-blue-200 cursor-pointer",
                !hasBroadcast && "text-gray-300 cursor-not-allowed",
              )}
            >
              {day.getDate()}
            </button>
          );
        })}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Verify TypeScript compilation**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/domain/playlists/DatePicker.tsx
git commit -m "feat(calendar): add year/month/day zoom navigation to DatePicker"
```

---

### Task 7: Create StationEventTable component

**Files:**
- Create: `frontend/src/components/domain/stations/StationEventTable.tsx`

- [ ] **Step 1: Create the component**

Write `frontend/src/components/domain/stations/StationEventTable.tsx`:

```tsx
import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { MatchStatusBadge } from "@/components/ui/Badge";
import { Spinner } from "@/components/ui/Spinner";
import { EmptyState } from "@/components/ui/EmptyState";
import { useStationEvents } from "@/api/stations";
import { formatDateTime } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PAGE_SIZE = 50;

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface StationEventTableProps {
  stationId: string;
  date: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function StationEventTable({ stationId, date }: StationEventTableProps) {
  const [offset, setOffset] = useState(0);

  // Reset to first page when date changes
  useEffect(() => {
    setOffset(0);
  }, [date]);

  const { data, isLoading, isError } = useStationEvents(
    stationId,
    date,
    PAGE_SIZE,
    offset,
  );

  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const totalPages = data ? Math.ceil(data.total / PAGE_SIZE) : 1;

  const handlePrev = () => setOffset((o) => Math.max(0, o - PAGE_SIZE));
  const handleNext = () =>
    setOffset((o) => (o + PAGE_SIZE < (data?.total ?? 0) ? o + PAGE_SIZE : o));

  // -------------------------------------------------------------------------
  // Loading / error states
  // -------------------------------------------------------------------------

  if (isLoading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="h-8 w-8 text-indigo-500" />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <p className="rounded-md bg-red-50 p-4 text-sm text-red-700">
        Failed to load broadcast events.
      </p>
    );
  }

  if (data.items.length === 0 && offset === 0) {
    return (
      <EmptyState
        title="No events"
        description="No broadcast events recorded for this date."
      />
    );
  }

  // -------------------------------------------------------------------------
  // Main render
  // -------------------------------------------------------------------------

  return (
    <div className="flex flex-col gap-3">
      {/* Total count header */}
      <div className="flex items-center justify-between">
        <p className="text-sm text-gray-500">
          {data.total.toLocaleString()} events total
        </p>
        <p className="text-sm text-gray-400">
          Page {page} of {totalPages}
        </p>
      </div>

      {/* Table */}
      <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white shadow-sm">
        <table className="min-w-full divide-y divide-gray-200 text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="px-4 py-3 text-left font-medium text-gray-500">
                Time
              </th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">
                Playlist
              </th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">
                Artist
              </th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">
                Title
              </th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">
                Status
              </th>
              <th className="px-4 py-3 text-left font-medium text-gray-500">
                Tier
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {data.items.map((event) => (
              <tr key={event.id} className="hover:bg-gray-50">
                <td className="whitespace-nowrap px-4 py-3 text-gray-600">
                  {formatDateTime(event.played_at)}
                </td>
                <td className="whitespace-nowrap px-4 py-3 text-gray-500">
                  {event.playlist_name}
                </td>
                <td className="px-4 py-3 text-gray-900">
                  {event.artist_name}
                </td>
                <td className="px-4 py-3 text-gray-900">{event.title}</td>
                <td className="px-4 py-3">
                  <MatchStatusBadge status={event.match_status} />
                </td>
                <td className="px-4 py-3 text-gray-500">
                  {event.match_tier ?? (
                    <span className="text-gray-300">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Pagination controls */}
      <div className="flex items-center justify-end gap-2">
        <button
          type="button"
          onClick={handlePrev}
          disabled={offset === 0}
          className="inline-flex items-center gap-1 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
          aria-label="Previous page"
        >
          <ChevronLeft className="h-4 w-4" />
          Previous
        </button>
        <button
          type="button"
          onClick={handleNext}
          disabled={offset + PAGE_SIZE >= data.total}
          className="inline-flex items-center gap-1 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
          aria-label="Next page"
        >
          Next
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Verify TypeScript compilation**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/domain/stations/StationEventTable.tsx
git commit -m "feat(frontend): add StationEventTable component for station+date events"
```

---

### Task 8: Simplify PlaylistViewer

**Files:**
- Modify: `frontend/src/pages/stations/PlaylistViewer.tsx:1-189`

- [ ] **Step 1: Rewrite PlaylistViewer**

Replace the full file `frontend/src/pages/stations/PlaylistViewer.tsx`:

```tsx
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Download } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { EmptyState } from "@/components/ui/EmptyState";
import { StationEventTable } from "@/components/domain/stations/StationEventTable";
import { DatePicker } from "@/components/domain/playlists/DatePicker";
import {
  useStation,
  useStationBroadcastDays,
  useExportStationM3u,
} from "@/api/stations";

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function PlaylistViewer() {
  const { station_id } = useParams<{ station_id: string }>();

  const { data: station, isLoading: stationLoading } = useStation(station_id);
  const { data: broadcastDays = [] } = useStationBroadcastDays(station_id);

  const [calendarMonth, setCalendarMonth] = useState(() => new Date());
  const [selectedDate, setSelectedDate] = useState<string | undefined>(
    undefined,
  );

  const exportMutation = useExportStationM3u();

  // -------------------------------------------------------------------------
  // Loading state
  // -------------------------------------------------------------------------

  if (stationLoading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="h-8 w-8 text-indigo-500" />
      </div>
    );
  }

  // -------------------------------------------------------------------------
  // Derived values
  // -------------------------------------------------------------------------

  const displayTitle = station
    ? station.name
      ? `${station.call_letters} — ${station.name}`
      : station.call_letters
    : "Broadcasts";

  const handleExport = () => {
    if (!station_id || !selectedDate || !station) return;
    exportMutation.mutate({
      stationId: station_id,
      date: selectedDate,
      callLetters: station.call_letters,
    });
  };

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  return (
    <div className="space-y-6">
      {/* Back link */}
      <Link
        to={station_id ? `/stations/${station_id}` : "/stations"}
        className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to Station
      </Link>

      {/* Page header */}
      <PageHeader
        title={displayTitle}
        description="Broadcast calendar"
        actions={
          <button
            type="button"
            onClick={handleExport}
            disabled={!selectedDate || exportMutation.isPending}
            className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {exportMutation.isPending ? (
              <Spinner className="h-4 w-4" />
            ) : (
              <Download className="h-4 w-4" />
            )}
            {selectedDate ? `Export M3U for ${selectedDate}` : "Export M3U"}
          </button>
        }
      />

      {/* Main two-column layout */}
      <div className="flex gap-6 items-start">
        {/* Left sidebar — calendar only */}
        <aside className="w-80 shrink-0">
          <DatePicker
            broadcastDays={broadcastDays}
            selectedDate={selectedDate}
            onSelect={setSelectedDate}
            month={calendarMonth}
            onMonthChange={setCalendarMonth}
          />
        </aside>

        {/* Right content */}
        <div className="flex-1 min-w-0">
          {selectedDate && station_id ? (
            <>
              <p className="mb-3 text-sm font-medium text-gray-700">
                {selectedDate}
              </p>
              <StationEventTable stationId={station_id} date={selectedDate} />
            </>
          ) : (
            <EmptyState
              title="Select a date"
              description="Select a date from the calendar to view broadcasts."
            />
          )}
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Verify TypeScript compilation**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/stations/PlaylistViewer.tsx
git commit -m "refactor(frontend): simplify PlaylistViewer to use station-level calendar"
```

---

### Task 9: Add uploaded playlists section to StationDashboard

**Files:**
- Modify: `frontend/src/pages/stations/StationDashboard.tsx:265-333`

- [ ] **Step 1: Add playlist import and section**

In `frontend/src/pages/stations/StationDashboard.tsx`, add the import for `usePlaylists` at the top (merge with existing imports around line 18):

```typescript
import { usePlaylists } from "@/api/playlists";
```

Add the `ListMusic` icon to the lucide-react import (line 3-11) — it's already imported, so no change needed there.

Inside the `StationDashboard` component, add the playlists query after the existing hooks (after line 39):

```typescript
  const { data: playlists } = usePlaylists(station_id);
```

Then, after the CSV upload `</div>` closing tag (after the upload section around line 333), add this new section:

```tsx
      {/* Uploaded playlists */}
      {playlists && playlists.length > 0 && (
        <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
          <h2 className="mb-3 text-sm font-semibold text-gray-900">
            Uploaded Playlists
          </h2>
          <ul className="divide-y divide-gray-100">
            {playlists.map((playlist) => (
              <li
                key={playlist.id}
                className="flex items-center justify-between py-2.5"
              >
                <span className="text-sm text-gray-700 truncate">
                  {playlist.name}
                </span>
                <span className="text-xs text-gray-400 shrink-0 ml-3">
                  {playlist.event_count.toLocaleString()} events
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
```

- [ ] **Step 2: Verify TypeScript compilation**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/stations/StationDashboard.tsx
git commit -m "feat(dashboard): add uploaded playlists inventory section"
```

---

### Task 10: Final verification

- [ ] **Step 1: Run all backend tests**

Run: `python -m pytest tests/ -v --ignore=tests/integration`
Expected: All tests PASS

- [ ] **Step 2: Run full frontend type check**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 3: Manual smoke test checklist**

Start the backend and frontend dev servers, then verify:

1. Open a station's playlist viewer page
2. Calendar shows year grid on load (only years with data)
3. Click a year → month grid appears, months without data are grayed out
4. Click the year header → returns to year grid
5. Click a month with data → day grid appears
6. Click "Month Year" header → returns to month grid
7. Click chevrons at day level → months navigate, including across year boundaries
8. Click a blue (broadcast) day → events load in the right panel with Playlist column
9. Events show correct playlist_name for each row
10. Pagination works on the events table
11. "Export M3U for {date}" button is disabled when no date selected, enabled when date selected
12. Export downloads a file named `{CALL_LETTERS}-{date}.m3u`
13. Station dashboard shows "Uploaded Playlists" section below CSV upload

- [ ] **Step 4: Commit any remaining fixes**

If any fixes were needed during smoke testing, commit them.

"""Characterisation, artist model phase 0a (SPEC S1): Reject and Unmatch of a log name, and
their cascade on its lines.

Phase 1b changes both on purpose (M24, AUD-R037: never take manual or same-name songs off air,
after a confirmation with the off-air count) and relocks this file. Until then it records
today's cascades, read only through ``play_file_resolution``:

- Reject (POST /matching/artists/{id}/resolve, manual_rejected) takes every line of the name
  off air except the manually matched ones, so the same-name song goes;
- Unmatch (POST /matching/artists/{id}/unmatch) takes every line off air, manual ones too.

The log name and its lines come from the ingestion task; the name is resolved by hand through
the route; its songs are matched by the identity matching task (MusicBrainz is never reached),
then one of them by hand through the route. The library comes from the scan.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import NoReturn
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import ID3, TIT2, TPE1
from psycopg.rows import DictRow, dict_row

from backend.config import get_settings
from backend.tasks import identity_matching_tasks
from backend.tasks.ingestion_tasks import ingestion_task
from tests.integration import stream_seed as seed
from tests.integration.missing_file_seed import full_scan, put
from tests.lifespan_client import lifespan_client

type Conn = psycopg.Connection[DictRow]

# psycopg's async pool needs the selector event loop on Windows (as tests/routers/conftest.py).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(scope="module")
def app_client(_migrated_db_url: str) -> Iterator[TestClient]:
    """The app on the test database, its lifespan run once per module; only the token is bypassed.

    DATABASE_URL names the test database for the whole module, so the task entry points use it.
    """
    patch = pytest.MonkeyPatch()
    patch.setenv("DATABASE_URL", _migrated_db_url)
    patch.setenv("RETROSTATION_SKIP_BOOT_MIGRATIONS", "1")
    get_settings.cache_clear()
    from backend.dependencies import get_current_token
    from backend.main import app

    async def _token() -> str:
        return "test-token"

    app.dependency_overrides[get_current_token] = _token
    try:
        with lifespan_client(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        patch.undo()


@pytest.fixture
def client(app_client: TestClient, migrated_db: str) -> TestClient:
    """The module's client; tables are truncated before each test."""
    return app_client


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


class _NoMusicBrainz:
    """A MusicBrainz client the seeds must never call: any call fails the test."""

    live_fetches = 0
    cache_hits = 0

    def __getattr__(self, name: str) -> NoReturn:
        raise AssertionError(f"the seed reached MusicBrainz ({name})")


def _tagged(dest: Path, artist: str, title: str) -> None:
    put("well_tagged.mp3", dest)
    tags = ID3(str(dest))
    tags.clear()
    tags.add(TPE1(encoding=3, text=[artist]))
    tags.add(TIT2(encoding=3, text=[title]))
    tags.save()


def _file_named(conn: Conn, name: str) -> UUID:
    rows = conn.execute("SELECT id, file_path FROM library_files").fetchall()
    [file_id] = [UUID(str(r["id"])) for r in rows if PureWindowsPath(r["file_path"]).name == name]
    return file_id


def _plays(conn: Conn, event: UUID) -> UUID | None:
    """The file *event* resolves to, read only through the view; None when nothing plays."""
    [row] = conn.execute(
        "SELECT file_id FROM play_file_resolution WHERE play_event_id = %s", (event,)
    ).fetchall()
    return None if row["file_id"] is None else UUID(str(row["file_id"]))


_LOG = b"""Artist,Title,Played
CASCADE BAND,SONG ONE,1995-03-14 08:00:00
CASCADE BAND,SONG TWO,1995-03-14 08:04:00
"""


@dataclass(frozen=True)
class _LogName:
    """CASCADE BAND at an AC station: SONG ONE auto-matched, SONG TWO matched by hand."""

    id: UUID
    song_one_play: UUID
    song_one_file: UUID
    song_two_play: UUID
    song_two_file: UUID


def _row(conn: Conn, query: str, *params: object) -> DictRow:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row


@pytest.fixture
def cascade_band(
    conn: Conn, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _LogName:
    root = tmp_path / "library"
    _tagged(root / "song-one.mp3", "Cascade Band", "Song One")
    _tagged(root / "song-two.mp3", "Cascade Band", "Song Two")
    full_scan(conn, root)
    station = seed.station(conn, "AC")
    conn.commit()
    ingestion_task.call_local(_LOG, "cascade-band.csv", str(station), "phase-0a-cascade")

    playlist = _row(conn, "SELECT id FROM playlists WHERE station_id = %s", station)["id"]
    name = UUID(str(_row(conn, "SELECT id FROM broadcast_artists")["id"]))
    catalog = client.get("/api/v1/library/artists", params={"search": "Cascade Band"})
    [catalog_id] = [a["id"] for a in catalog.json()["items"] if a["name"] == "Cascade Band"]
    artist = client.post(
        f"/api/v1/matching/artists/{name}/resolve",
        json={"match_status": "manual_matched", "target_artist_id": catalog_id},
    )
    assert artist.status_code == 200
    monkeypatch.setattr(
        identity_matching_tasks,
        "MusicBrainzApiClient",
        lambda *_a, **_k: nullcontext(_NoMusicBrainz()),
    )
    identity_matching_tasks.identity_matching_task.call_local(str(playlist))

    def line(title: str) -> tuple[UUID, UUID]:
        row = _row(
            conn,
            """SELECT pe.id AS play, ti.id AS line FROM play_events pe
               JOIN track_identities ti ON ti.id = pe.identity_id
               WHERE ti.original_title = %s""",
            title,
        )
        return UUID(str(row["play"])), UUID(str(row["line"]))

    (one_play, _), (two_play, two_line) = line("SONG ONE"), line("SONG TWO")
    by_hand = client.post(
        f"/api/v1/matching/identities/{two_line}/resolve",
        json={
            "match_status": "manual_matched",
            "library_file_id": str(_file_named(conn, "song-two.mp3")),
        },
    )
    assert by_hand.status_code == 200
    conn.commit()
    return _LogName(
        id=name,
        song_one_play=one_play,
        song_one_file=_file_named(conn, "song-one.mp3"),
        song_two_play=two_play,
        song_two_file=_file_named(conn, "song-two.mp3"),
    )


def _on_air(conn: Conn, name: _LogName) -> tuple[UUID | None, UUID | None]:
    return _plays(conn, name.song_one_play), _plays(conn, name.song_two_play)


def test_reject_of_a_log_name(conn: Conn, client: TestClient, cascade_band: _LogName) -> None:
    """S1, Reject of a log name: the auto-matched same-name song goes off air, the song
    matched by hand stays."""
    assert _on_air(conn, cascade_band) == (cascade_band.song_one_file, cascade_band.song_two_file)

    response = client.post(
        f"/api/v1/matching/artists/{cascade_band.id}/resolve",
        json={"match_status": "manual_rejected"},
    )

    assert response.status_code == 200
    assert _on_air(conn, cascade_band) == (None, cascade_band.song_two_file)


def test_unmatch_of_a_log_name(conn: Conn, client: TestClient, cascade_band: _LogName) -> None:
    """S1, Unmatch of a log name: every line goes off air, the one matched by hand too."""
    assert _on_air(conn, cascade_band) == (cascade_band.song_one_file, cascade_band.song_two_file)

    response = client.post(f"/api/v1/matching/artists/{cascade_band.id}/unmatch")

    assert response.status_code == 200
    assert _on_air(conn, cascade_band) == (None, None)

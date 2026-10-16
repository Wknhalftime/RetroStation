"""Acceptance tests: both M3U exports leave out a silent file (S7), on PostgreSQL.

Through the HTTP endpoints, as tests/routers/test_m3u_export.py does: a station-day export and
a playlist export, each with a silent master between two playable ones. The silent play gets
no line; its neighbours keep theirs, in order. A file with no cue row is not silent.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from tests.integration import stream_seed as seed
from tests.integration.stream_seed import DAY, Conn, at

SILENT_ROW: dict[str, Any] = {
    "cue_in_ms": 0,
    "cue_out_ms": 200_000,
    "fade_in_ms": 3_000,
    "fade_out_ms": 4_000,
    "start_next_ms": 0,
    "gain_db": -8.0,
    "loudness_lufs": None,
    "analysis_failed": True,
    "audio_silent": True,
}


def silent_master(conn: Conn) -> UUID:
    audio = seed.audio_hash()
    file_id = seed.mastered_file(conn, audio_hash=audio)
    seed.cue_row(conn, audio, **SILENT_ROW)
    return file_id


def paths(m3u: str) -> list[str]:
    return [line for line in m3u.splitlines() if line and not line.startswith("#")]


def three_plays(conn: Conn) -> tuple[UUID, UUID, list[str]]:
    """A station and playlist playing a cued master, a silent one and an uncued one; the
    station, the playlist and the paths an export must write."""
    station = seed.station(conn)
    playlist = seed.playlist(conn, station)
    before, _ = seed.cued_master(conn)
    silent = silent_master(conn)
    after = seed.mastered_file(conn)
    for hms, file_id in (("08:00", before), ("08:05", silent), ("08:10", after)):
        seed.matched_play(conn, playlist, at(hms), file_id)
    conn.commit()
    return station, playlist, [seed.file_path(conn, before), seed.file_path(conn, after)]


def test_the_station_export_leaves_out_a_silent_file(client: TestClient, db_conn: Conn) -> None:
    station, _, expected = three_plays(db_conn)
    resp = client.post(f"/api/v1/stations/{station}/export-m3u", json={"date": DAY.isoformat()})
    assert resp.status_code == 200
    assert paths(resp.text) == expected


def test_the_playlist_export_leaves_out_a_silent_file(client: TestClient, db_conn: Conn) -> None:
    _, playlist, expected = three_plays(db_conn)
    resp = client.post(f"/api/v1/playlists/{playlist}/export-m3u")
    assert resp.status_code == 200
    assert paths(resp.text) == expected

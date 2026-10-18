"""Acceptance tests: GET /api/v1/library/silent-files (S6), on PostgreSQL.

A thin router: one service call, StorageUnavailableError -> 503. Each file shows artist,
title, album, path, duration and why it is listed; silent files first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest

from backend.domain.system import StorageUnavailableError
from tests.integration import stream_seed as seed

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

URL = "/api/v1/library/silent-files"


def tagged(
    db_conn: Any, audio: str, duration_ms: int, file_size: int, tags: tuple[str, ...]
) -> UUID:
    file_id = seed.library_file(
        db_conn, duration_ms=duration_ms, file_size=file_size, audio_hash=audio
    )
    db_conn.execute(
        """UPDATE library_files SET artist_name = %s, track_title = %s, release_title = %s
           WHERE id = %s""",
        (*tags, file_id),
    )
    return file_id


def test_lists_silent_files_first_with_their_facts(client: TestClient, db_conn: Any) -> None:
    testimony_audio, blank_audio = seed.audio_hash(), seed.audio_hash()
    testimony = tagged(
        db_conn, testimony_audio, 298_666, 632_875, ("Mary J. Blige", "Testimony", "No More Drama")
    )
    seed.cue_row(db_conn, testimony_audio, cue_in_ms=188_500, cue_out_ms=200_200, start_next_ms=0)
    blank = tagged(db_conn, blank_audio, 6_000, 1_000, ("No Artist", "Blank", "HitDisc 883B"))
    seed.cue_row(
        db_conn,
        blank_audio,
        cue_in_ms=0,
        cue_out_ms=6_000,
        fade_in_ms=3_000,
        fade_out_ms=4_000,
        start_next_ms=0,
        gain_db=-8.0,
        loudness_lufs=None,
        analysis_failed=True,
        audio_silent=True,
    )
    seed.cued_file(db_conn)  # an ordinary song: not listed
    db_conn.commit()

    body = client.get(URL).json()

    assert (body["silent"], body["nearly_silent"]) == (1, 1)
    assert body["files"] == [
        {
            "file_id": str(blank),
            "artist": "No Artist",
            "title": "Blank",
            "album": "HitDisc 883B",
            "path": seed.file_path(db_conn, blank),
            "duration_ms": 6_000,
            "reasons": ["silent"],
            "bitrate_kbps": 1,
            "cued_span_ms": None,
        },
        {
            "file_id": str(testimony),
            "artist": "Mary J. Blige",
            "title": "Testimony",
            "album": "No More Drama",
            "path": seed.file_path(db_conn, testimony),
            "duration_ms": 298_666,
            "reasons": ["low_bitrate", "short_cued_span"],
            "bitrate_kbps": 17,
            "cued_span_ms": 11_700,
        },
    ]


def test_an_empty_report(client: TestClient) -> None:
    response = client.get(URL)
    assert response.status_code == 200
    assert response.json() == {"files": [], "silent": 0, "nearly_silent": 0}


def test_a_lost_connection_answers_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.routers.library import silent_files as route

    def lost(_report: object) -> None:
        raise StorageUnavailableError("the database connection was lost")

    monkeypatch.setattr(route, "silent_file_report", lost)
    assert client.get(URL).status_code == 503

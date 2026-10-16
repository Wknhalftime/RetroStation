"""Acceptance tests: the Library silent-files read on PostgreSQL (S6).

PgSilentFileReportRepository.candidates pre-filters present files with the domain's constants:
a silent cue row (any length), or a file over 30 s under 64 kb/s on disk, or one whose real
cue row spans under 10 % of it. Every row it offers has a reason, every file with a reason is
offered, and nothing else: no missing file, no fallback row's span. Reached through
RepositoryFactory, as production wires it; read as attributes, so each test fails alone.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain import streaming
from backend.services.repository_factory import RepositoryFactory
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import Conn

SILENT_ROW: dict[str, Any] = {
    "cue_in_ms": 0,
    "cue_out_ms": 6_000,
    "fade_in_ms": 3_000,
    "fade_out_ms": 4_000,
    "start_next_ms": 0,
    "gain_db": -8.0,
    "loudness_lufs": None,
    "analysis_failed": True,
    "audio_silent": True,
}


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def tagged(
    conn: Conn,
    *,
    duration_ms: int = 200_000,
    file_size: int = 4_000_000,
    audio: str | None = None,
    status: str = "present",
    tags: tuple[str, str, str] = ("Artist", "Title", "Album"),
) -> UUID:
    file_id = seed.library_file(
        conn, status=status, duration_ms=duration_ms, file_size=file_size, audio_hash=audio
    )
    conn.execute(
        """UPDATE library_files SET artist_name = %s, track_title = %s, release_title = %s
           WHERE id = %s""",
        (*tags, file_id),
    )
    return file_id


def cued(conn: Conn, cue_in_ms: int, cue_out_ms: int, **file: Any) -> UUID:
    """A tagged file with a real (analysed) cue row spanning cue_in_ms..cue_out_ms."""
    audio = seed.audio_hash()
    file_id = tagged(conn, audio=audio, **file)
    seed.cue_row(conn, audio, cue_in_ms=cue_in_ms, cue_out_ms=cue_out_ms, start_next_ms=0)
    return file_id


def silent(conn: Conn, **file: Any) -> UUID:
    audio = seed.audio_hash()
    file_id = tagged(conn, audio=audio, duration_ms=6_000, file_size=1_000, **file)
    seed.cue_row(conn, audio, **SILENT_ROW)
    return file_id


def candidates(conn: Conn) -> dict[UUID, Any]:
    return {e.file_id: e for e in RepositoryFactory(conn).streaming.silent_files.candidates()}


def test_the_report_reads_each_listed_file_and_nothing_else(conn: Conn) -> None:
    reason = streaming.SilenceReason
    failed_audio = seed.audio_hash()
    failed_short = tagged(conn, audio=failed_audio)
    seed.cue_row(
        conn, failed_audio, cue_in_ms=0, cue_out_ms=1_000, start_next_ms=0, analysis_failed=True
    )
    expected = {
        silent(conn): (reason.SILENT,),
        cued(conn, 188_500, 200_200, duration_ms=298_666, file_size=632_875): (
            reason.LOW_BITRATE,
            reason.SHORT_CUED_SPAN,
        ),
        cued(conn, 1_000, 181_000, file_size=1_000_000): (reason.LOW_BITRATE,),
        cued(conn, 0, 19_999): (reason.SHORT_CUED_SPAN,),
        tagged(conn, file_size=1_000_000): (reason.LOW_BITRATE,),  # no cue row yet
    }
    not_listed = {
        cued(conn, 1_000, 181_000, file_size=1_600_000),  # exactly 64 kb/s
        cued(conn, 0, 20_000),  # exactly 10 %
        tagged(conn, duration_ms=30_000, file_size=1_000),  # not over 30 s
        failed_short,  # a fallback row's span is no signal
        cued(conn, 1_000, 181_000),  # an ordinary song
        silent(conn, status="missing"),  # not present
    }
    got = candidates(conn)
    assert {file_id: e.reasons() for file_id, e in got.items()} == expected
    assert not_listed.isdisjoint(got)


def test_a_listed_file_carries_its_facts(conn: Conn) -> None:
    testimony = cued(
        conn,
        188_500,
        200_200,
        duration_ms=298_666,
        file_size=632_875,
        tags=("Mary J. Blige", "Testimony", "No More Drama"),
    )
    got = candidates(conn)[testimony]
    assert (
        got.artist,
        got.title,
        got.album,
        got.path,
        got.duration_ms,
        got.file_size,
        got.audio_silent,
        got.cued_span_ms,
        got.bitrate_kbps(),
    ) == (
        "Mary J. Blige",
        "Testimony",
        "No More Drama",
        seed.file_path(conn, testimony),
        298_666,
        632_875,
        False,
        11_700,
        17,
    )


def test_twins_of_silent_audio_are_both_listed(conn: Conn) -> None:
    """Each path must be replaced; the marker is the audio's (S3)."""
    audio = seed.audio_hash()
    first = tagged(conn, audio=audio, duration_ms=6_000, file_size=1_000)
    second = tagged(conn, audio=audio, duration_ms=6_000, file_size=1_000)
    seed.cue_row(conn, audio, **SILENT_ROW)
    got = candidates(conn)
    assert set(got) == {first, second}
    assert all(e.audio_silent and e.cued_span_ms is None for e in got.values())

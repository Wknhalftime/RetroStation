"""Cue coverage on PostgreSQL (PR G2, Task 6; traceability K: T6.1-T6.5).

Requirements: D77 and D89 (the "cues ready X of Y" count); PG7 (the coverage definitions:
analysable, ready, failed and unhashed); D20 ("needs analysis" is defined once: a present file
with an audio hash whose audio has no ``stream_cues`` row; a row means the audio has cues,
whether analysed or a fallback); D21 (present means ``file_status = 'present'``); D52/D56 (a
failed analysis stores a fallback row, counted apart from a real one; a row for audio no
longer in the library is harmless and not counted); H8 (one owner per fact).

Counts are per audio, not per file: twins share one cue row (D20), so they are one audio.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.stream_cue_coverage import PgCueCoverageRepository
from backend.db.repositories.stream_cue_work import PgCueWorkRepository
from backend.domain.streaming import CueCoverage
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import Conn


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def hashed(conn: Conn, *, status: str = "present", audio: str | None = None) -> str:
    """A library file with ``audio`` (a fresh hash when None); its hash."""
    audio = audio or seed.audio_hash()
    seed.library_file(conn, audio_hash=audio, status=status)
    return audio


def test_each_present_audio_is_counted_once(conn: Conn) -> None:
    # T6.1 (D77, PG7, D20, D21): twins are one audio; a missing file's audio is not counted.
    shared = hashed(conn)
    hashed(conn, audio=shared)
    hashed(conn)
    hashed(conn, status="missing")
    assert PgCueCoverageRepository(conn).coverage() == CueCoverage(
        analysable=2, ready=0, failed=0, unhashed=0
    )


def test_ready_and_failed_audio_are_counted_apart(conn: Conn) -> None:
    # T6.2 (PG7, D52/D56): a real row is ready, a fallback row is failed; a row for audio no
    # present file carries (an orphan, or a missing file's) is not counted at all. Twins on
    # settled audio are still one audio each (audit SF3).
    _, cued = seed.cued_file(conn)
    hashed(conn, audio=cued)
    failed = hashed(conn)
    hashed(conn, audio=failed)
    seed.cue_row(conn, failed, analysis_failed=True)
    hashed(conn)
    seed.cue_row(conn, seed.audio_hash())
    seed.cued_file(conn, status="missing")
    assert PgCueCoverageRepository(conn).coverage() == CueCoverage(
        analysable=3, ready=1, failed=1, unhashed=0
    )


def test_present_files_without_a_fingerprint_are_counted_as_unhashed(conn: Conn) -> None:
    # T6.3 (PG7, D20: a file with no audio_hash has no cues, ever): counted per file, apart
    # from the analysable audio; a missing file is not counted.
    seed.library_file(conn)
    seed.library_file(conn)
    seed.library_file(conn, status="missing")
    hashed(conn)
    assert PgCueCoverageRepository(conn).coverage() == CueCoverage(
        analysable=1, ready=0, failed=0, unhashed=2
    )


def test_what_is_waiting_is_exactly_what_needs_analysis(conn: Conn) -> None:
    # T6.4 (D20, H8): the audio waiting is exactly the audio cue pre-computation would
    # analyse, read through E1's own "needs analysis" reader. A row the daily prune marked
    # orphaned (``orphaned_at``) whose audio is present again still settles it: "needs
    # analysis" has no such filter (migration 0033; audit SF4).
    seed.cued_file(conn)
    returned = hashed(conn)
    seed.cue_row(conn, returned, orphaned_at=datetime.now(UTC))
    failed = hashed(conn)
    seed.cue_row(conn, failed, analysis_failed=True)
    twin = hashed(conn)
    hashed(conn, audio=twin)
    hashed(conn)
    hashed(conn, status="missing")
    seed.library_file(conn)
    needing = {c.audio_hash for c in PgCueWorkRepository(conn).library(None, 1_000)}
    coverage = PgCueCoverageRepository(conn).coverage()
    assert coverage.waiting == len(needing) == 2
    assert coverage.settled == 3


def test_an_empty_library_counts_nothing(conn: Conn) -> None:
    # T6.5 (PG7), guard: nothing to analyse is four zeros, never an error.
    assert PgCueCoverageRepository(conn).coverage() == CueCoverage(
        analysable=0, ready=0, failed=0, unhashed=0
    )

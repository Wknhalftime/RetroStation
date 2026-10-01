"""The reported file, read against PostgreSQL (spec: D79, the owner analyses a reported song's
audio only while it has no data; D20, "needs analysis" is "has an audio_hash and no
stream_cues row"; D21, present only; D61, a failed row is not analysed again)."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.stream_cue_work import PgCueWorkRepository
from backend.domain.library import AudioHash
from backend.domain.streaming import CueCandidate
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import Conn

pytestmark = pytest.mark.integration


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def test_a_reported_file_whose_audio_has_no_row_is_its_candidate(conn: Conn) -> None:
    """Everything the analysis needs: the hash to re-check, the duration for a fallback row
    (D52), the stat the hash is trusted by (D57)."""
    audio = seed.audio_hash()
    file_id = seed.library_file(conn, audio_hash=audio, duration_ms=None)
    assert PgCueWorkRepository(conn).reported(file_id) == CueCandidate(
        file_id=file_id,
        path=seed.file_path(conn, file_id),
        audio_hash=AudioHash.parse(audio),
        duration_ms=None,
        file_size=seed.FILE_SIZE,
        file_mtime_ns=seed.FILE_MTIME_NS,
    )


@pytest.mark.parametrize("failed", [False, True], ids=["analysed", "failed row"])
def test_nothing_to_analyse_once_its_audio_has_a_row(conn: Conn, failed: bool) -> None:
    """D20 and D61: a row of either kind is data; a twin's row counts too (same audio)."""
    audio = seed.audio_hash()
    file_id = seed.library_file(conn, audio_hash=audio)
    seed.cue_row(conn, audio, analysis_failed=failed)
    assert PgCueWorkRepository(conn).reported(file_id) is None


def test_nothing_to_analyse_without_an_audio_hash(conn: Conn) -> None:
    """D20: "A file with no audio_hash has no cues"."""
    file_id = seed.library_file(conn)
    assert PgCueWorkRepository(conn).reported(file_id) is None


@pytest.mark.parametrize("status", ["missing", "deleted"])
def test_nothing_to_analyse_for_a_file_that_is_not_present(conn: Conn, status: str) -> None:
    """D21: playable, and analysable, means present."""
    file_id = seed.library_file(conn, audio_hash=seed.audio_hash(), status=status)
    assert PgCueWorkRepository(conn).reported(file_id) is None


def test_nothing_to_analyse_for_an_unknown_file(conn: Conn) -> None:
    """A file deleted from the library since it was reported."""
    assert PgCueWorkRepository(conn).reported(uuid4()) is None

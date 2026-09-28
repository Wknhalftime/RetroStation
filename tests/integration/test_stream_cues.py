"""Acceptance tests: ``PgStreamCueRepository.upsert`` (spec: Data)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.stream_cues import PgStreamCueRepository
from backend.domain.streaming import CueAnalysis, CueFileNotFoundError, CuePoints
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import FILE_MTIME_NS, FILE_SIZE, VERSION, Conn

CUES = CuePoints(
    cue_in_ms=1_500,
    cue_out_ms=201_000,
    fade_in_ms=2_500,
    fade_out_ms=4_500,
    start_next_ms=3_500,
    gain_db=-4.25,
)


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def analysis(file_id: UUID, **overrides: Any) -> CueAnalysis:
    fields: dict[str, Any] = {
        "file_id": file_id,
        "cues": CUES,
        "loudness_lufs": -13.5,
        "analysis_failed": False,
        "analyser_version": VERSION,
        "file_size": FILE_SIZE,
        "file_mtime_ns": FILE_MTIME_NS,
    }
    return CueAnalysis(**{**fields, **overrides})


def stored(conn: Conn, file_id: UUID) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM stream_cues WHERE library_file_id = %s", (file_id,)
    ).fetchall()
    assert len(rows) == 1
    return dict(rows[0])


def as_row(a: CueAnalysis) -> dict[str, Any]:
    """The columns an upsert of ``a`` must store (all but ``analysed_at``)."""
    return {
        "library_file_id": a.file_id,
        "cue_in_ms": a.cues.cue_in_ms,
        "cue_out_ms": a.cues.cue_out_ms,
        "fade_in_ms": a.cues.fade_in_ms,
        "fade_out_ms": a.cues.fade_out_ms,
        "start_next_ms": a.cues.start_next_ms,
        "loudness_lufs": a.loudness_lufs,
        "gain_db": a.cues.gain_db,
        "file_size": a.file_size,
        "file_mtime_ns": a.file_mtime_ns,
        "analyser_version": a.analyser_version,
        "analysis_failed": a.analysis_failed,
    }


def without_analysed_at(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k != "analysed_at"}


def test_upsert_stores_every_field(conn: Conn) -> None:
    a = analysis(seed.library_file(conn))

    PgStreamCueRepository(conn).upsert(a)

    assert without_analysed_at(stored(conn, a.file_id)) == as_row(a)


def test_upsert_stamps_analysed_at(conn: Conn) -> None:
    a = analysis(seed.library_file(conn))

    PgStreamCueRepository(conn).upsert(a)

    row = conn.execute(
        "SELECT analysed_at = now() AS stamped FROM stream_cues WHERE library_file_id = %s",
        (a.file_id,),
    ).fetchone()
    assert row == {"stamped": True}


def test_upsert_stores_unknown_loudness_and_stat_as_null(conn: Conn) -> None:
    a = analysis(seed.library_file(conn), loudness_lufs=None, file_size=None, file_mtime_ns=None)

    PgStreamCueRepository(conn).upsert(a)

    row = stored(conn, a.file_id)
    assert (row["loudness_lufs"], row["file_size"], row["file_mtime_ns"]) == (None, None, None)


def test_upsert_replaces_the_row_of_the_same_file_and_restamps_it(conn: Conn) -> None:
    file_id = seed.library_file(conn)
    repo = PgStreamCueRepository(conn)
    repo.upsert(analysis(file_id))
    long_ago = datetime(2000, 1, 1, tzinfo=UTC)
    conn.execute("UPDATE stream_cues SET analysed_at = %s", (long_ago,))
    again = analysis(
        file_id,
        cues=CuePoints(
            cue_in_ms=0,
            cue_out_ms=90_000,
            fade_in_ms=3_000,
            fade_out_ms=4_000,
            start_next_ms=4_000,
            gain_db=-8.0,
        ),
        loudness_lufs=None,
        analysis_failed=True,
        analyser_version=VERSION + 1,
        file_size=FILE_SIZE + 1,
        file_mtime_ns=FILE_MTIME_NS + 1,
    )

    repo.upsert(again)

    row = stored(conn, file_id)
    assert without_analysed_at(row) == as_row(again)
    assert row["analysed_at"] > long_ago


def test_upsert_keeps_other_files_rows(conn: Conn) -> None:
    first, second = seed.library_file(conn), seed.library_file(conn)
    repo = PgStreamCueRepository(conn)
    repo.upsert(analysis(first))
    repo.upsert(analysis(second, analysis_failed=True))

    assert stored(conn, first)["analysis_failed"] is False
    assert stored(conn, second)["analysis_failed"] is True


def test_upsert_for_a_file_that_does_not_exist_raises_cue_file_not_found(conn: Conn) -> None:
    ghost = uuid4()

    with pytest.raises(CueFileNotFoundError, match=str(ghost)) as raised:
        PgStreamCueRepository(conn).upsert(analysis(ghost))

    assert raised.value.file_id == ghost


def test_the_callers_transaction_survives_an_upsert_for_a_missing_file(conn: Conn) -> None:
    kept = seed.library_file(conn)  # opens the caller's transaction
    repo = PgStreamCueRepository(conn)

    with pytest.raises(CueFileNotFoundError):
        repo.upsert(analysis(uuid4()))

    repo.upsert(analysis(kept))
    assert stored(conn, kept)["library_file_id"] == kept

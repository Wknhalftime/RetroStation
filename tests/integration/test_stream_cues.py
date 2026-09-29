"""Acceptance tests: ``PgStreamCueRepository.upsert`` (spec: Data, D20).

One row per audio hash. An upsert needs no library file to exist: a row for audio no
longer (or not yet) in the library is harmless.

Approved acceptance tests for D20; locked in ``.claude/frozen-tests.json``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.stream_cues import PgStreamCueRepository
from backend.domain.library import AudioHash
from backend.domain.streaming import CueAnalysis, CuePoints
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import VERSION, Conn

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


def new_audio() -> AudioHash:
    return AudioHash.parse(seed.audio_hash())


def analysis(audio: AudioHash, **overrides: Any) -> CueAnalysis:
    fields: dict[str, Any] = {
        "audio_hash": audio,
        "cues": CUES,
        "loudness_lufs": -13.5,
        "analysis_failed": False,
        "analyser_version": VERSION,
    }
    return CueAnalysis(**{**fields, **overrides})


def stored(conn: Conn, audio: AudioHash) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM stream_cues WHERE audio_hash = %s", (str(audio),)).fetchall()
    assert len(rows) == 1
    return dict(rows[0])


def as_row(a: CueAnalysis) -> dict[str, Any]:
    """The columns an upsert of ``a`` must store (all but ``analysed_at``)."""
    return {
        "audio_hash": str(a.audio_hash),
        "cue_in_ms": a.cues.cue_in_ms,
        "cue_out_ms": a.cues.cue_out_ms,
        "fade_in_ms": a.cues.fade_in_ms,
        "fade_out_ms": a.cues.fade_out_ms,
        "start_next_ms": a.cues.start_next_ms,
        "loudness_lufs": a.loudness_lufs,
        "gain_db": a.cues.gain_db,
        "analyser_version": a.analyser_version,
        "analysis_failed": a.analysis_failed,
    }


def without_analysed_at(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k != "analysed_at"}


def test_upsert_stores_every_field(conn: Conn) -> None:
    audio = new_audio()
    seed.library_file(conn, audio_hash=str(audio))
    a = analysis(audio)

    PgStreamCueRepository(conn).upsert(a)

    assert without_analysed_at(stored(conn, audio)) == as_row(a)


def test_upsert_stamps_analysed_at(conn: Conn) -> None:
    a = analysis(new_audio())

    PgStreamCueRepository(conn).upsert(a)

    row = conn.execute(
        "SELECT analysed_at = now() AS stamped FROM stream_cues WHERE audio_hash = %s",
        (str(a.audio_hash),),
    ).fetchone()
    assert row == {"stamped": True}


def test_upsert_stores_unknown_loudness_as_null(conn: Conn) -> None:
    a = analysis(new_audio(), loudness_lufs=None)

    PgStreamCueRepository(conn).upsert(a)

    assert stored(conn, a.audio_hash)["loudness_lufs"] is None


def test_upsert_for_audio_no_file_has_succeeds(conn: Conn) -> None:
    """No library file carries this hash: the row is stored all the same (D20)."""
    a = analysis(new_audio())
    files = conn.execute(
        "SELECT count(*) AS n FROM library_files WHERE audio_hash = %s", (str(a.audio_hash),)
    ).fetchone()
    assert files == {"n": 0}

    PgStreamCueRepository(conn).upsert(a)

    assert without_analysed_at(stored(conn, a.audio_hash)) == as_row(a)


def test_upsert_replaces_the_row_of_the_same_audio_and_restamps_it(conn: Conn) -> None:
    audio = new_audio()
    repo = PgStreamCueRepository(conn)
    repo.upsert(analysis(audio))
    long_ago = datetime(2000, 1, 1, tzinfo=UTC)
    conn.execute("UPDATE stream_cues SET analysed_at = %s", (long_ago,))
    again = analysis(
        audio,
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
    )

    repo.upsert(again)

    row = stored(conn, audio)
    assert without_analysed_at(row) == as_row(again)
    assert row["analysed_at"] > long_ago


def test_upsert_keeps_other_audios_rows(conn: Conn) -> None:
    first, second = new_audio(), new_audio()
    repo = PgStreamCueRepository(conn)
    repo.upsert(analysis(first))
    repo.upsert(analysis(second, analysis_failed=True))

    assert stored(conn, first)["analysis_failed"] is False
    assert stored(conn, second)["analysis_failed"] is True


def test_both_hash_kinds_are_stored(conn: Conn) -> None:
    sha = AudioHash.parse("audio-sha256:" + "ab" * 32)

    PgStreamCueRepository(conn).upsert(analysis(sha))

    assert stored(conn, sha)["audio_hash"] == str(sha)

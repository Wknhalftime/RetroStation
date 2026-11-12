"""``stream_endings`` storage (EOM ending rules, stage 1, Task 4), against real PostgreSQL.

One row per audio: the ending type, the long-tail EOM, and the autocue Stop it was measured
against. ``needing_analysis`` lists the analysed audio a present library file carries that has
no ending yet, or one measured against an older Stop; liners are not songs and are never
listed. Spec: docs/superpowers/specs/2026-11-07-eom-ending-rules-design.md; plan Task 4.
"""

from __future__ import annotations

from collections.abc import Generator
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.db.repositories.stream_cues import PgStreamCueRepository
from backend.db.repositories.stream_endings import PgStreamEndingRepository
from backend.domain.endings import ENDING_ANALYSER_VERSION, EndingType, SongEnding
from backend.domain.library import AudioHash
from backend.domain.streaming import CueAnalysis, CuePoints

pytestmark = pytest.mark.integration

A = AudioHash.parse("flac-md5:" + "a" * 32)
B = AudioHash.parse("flac-md5:" + "b" * 32)
C = AudioHash.parse("flac-md5:" + "c" * 32)
D = AudioHash.parse("flac-md5:" + "d" * 32)

CUES = CuePoints(
    cue_in_ms=0,
    cue_out_ms=200_000,
    fade_in_ms=0,
    fade_out_ms=4_000,
    start_next_ms=5_000,
    gain_db=-6.0,
)


@pytest.fixture
def conn(migrated_db: str) -> Generator[psycopg.Connection[DictRow]]:
    """An autocommit dict-row connection with empty ``stream_endings`` and liners.

    The shared ``migrated_db`` truncation does not know these tables, so the fixture clears
    them before and after each test.
    """
    with psycopg.connect(migrated_db, autocommit=True, row_factory=dict_row) as c:
        c.execute("DELETE FROM stream_endings")
        c.execute("DELETE FROM station_liners")
        yield c
        c.execute("DELETE FROM stream_endings")
        c.execute("DELETE FROM station_liners")


def _file(
    conn: psycopg.Connection[DictRow], audio_hash: AudioHash, path: str, status: str = "present"
) -> None:
    conn.execute(
        """INSERT INTO library_files (id, file_path, audio_hash, duration_ms, file_status)
           VALUES (%s, %s, %s, 205000, %s)""",
        (uuid4(), path, str(audio_hash), status),
    )


def _cues(
    conn: psycopg.Connection[DictRow],
    audio_hash: AudioHash,
    *,
    failed: bool = False,
    silent: bool = False,
    cues: CuePoints = CUES,
) -> None:
    PgStreamCueRepository(conn).upsert(
        CueAnalysis(
            audio_hash=audio_hash,
            cues=cues,
            loudness_lufs=None if failed else -14.0,
            analysis_failed=failed or silent,
            analyser_version=1,
            audio_silent=silent,
        )
    )


def _song(
    conn: psycopg.Connection[DictRow],
    audio_hash: AudioHash,
    path: str | None = None,
    **cue_kwargs: object,
) -> None:
    _file(conn, audio_hash, path or f"/music/{audio_hash}.flac")
    _cues(conn, audio_hash, **cue_kwargs)  # type: ignore[arg-type]


def _repo(conn: psycopg.Connection[DictRow]) -> PgStreamEndingRepository:
    return PgStreamEndingRepository(conn)


@pytest.mark.parametrize(
    "ending",
    [
        SongEnding(EndingType.COLD, None, 200_000),
        SongEnding(EndingType.LONG_TAIL, 190_500, 200_000),
        SongEnding(EndingType.OTHER, None, 200_000),
        SongEnding(EndingType.UNMEASURED, None, 200_000),
    ],
    ids=lambda e: e.ending.value,
)
def test_store_and_get_round_trip(conn: psycopg.Connection[DictRow], ending: SongEnding) -> None:
    _song(conn, A)
    _repo(conn).store(A, ending)
    assert _repo(conn).get(A) == ending
    row = conn.execute("SELECT analyser_version FROM stream_endings").fetchone()
    assert row is not None and row["analyser_version"] == ENDING_ANALYSER_VERSION


def test_get_unknown_is_none(conn: psycopg.Connection[DictRow]) -> None:
    assert _repo(conn).get(A) is None


def test_store_replaces(conn: psycopg.Connection[DictRow]) -> None:
    _song(conn, A)
    _repo(conn).store(A, SongEnding(EndingType.OTHER, None, 200_000))
    _repo(conn).store(A, SongEnding(EndingType.LONG_TAIL, 190_500, 200_000))
    assert _repo(conn).get(A) == SongEnding(EndingType.LONG_TAIL, 190_500, 200_000)
    assert conn.execute("SELECT count(*) AS n FROM stream_endings").fetchone() == {"n": 1}


def test_needing_analysis_skips_failed_silent_and_measured(
    conn: psycopg.Connection[DictRow],
) -> None:
    _song(conn, A)
    _song(conn, B, failed=True)
    _song(conn, C, silent=True)
    _song(conn, D)
    _repo(conn).store(D, SongEnding(EndingType.OTHER, None, 200_000))
    found = _repo(conn).needing_analysis(None, 10)
    assert [c.audio_hash for c in found] == [A]
    assert found[0].cues == CUES
    assert found[0].path == f"/music/{A}.flac"


def test_needing_analysis_skips_audio_without_a_present_file(
    conn: psycopg.Connection[DictRow],
) -> None:
    _file(conn, A, "/music/gone.flac", status="missing")
    _cues(conn, A)
    assert _repo(conn).needing_analysis(None, 10) == []


def test_needing_analysis_returns_stale_measurement(conn: psycopg.Connection[DictRow]) -> None:
    _song(conn, A)
    _repo(conn).store(A, SongEnding(EndingType.OTHER, None, 199_000))  # an older Stop
    assert [c.audio_hash for c in _repo(conn).needing_analysis(None, 10)] == [A]


def test_needing_analysis_one_path_per_audio(conn: psycopg.Connection[DictRow]) -> None:
    _song(conn, A, "/music/b-twin.flac")
    _file(conn, A, "/music/a-twin.flac")
    found = _repo(conn).needing_analysis(None, 10)
    assert [(c.audio_hash, c.path) for c in found] == [(A, "/music/a-twin.flac")]


def test_needing_analysis_keyset_order_and_limit(conn: psycopg.Connection[DictRow]) -> None:
    for audio_hash in (D, B, A, C):
        _song(conn, audio_hash)
    repo = _repo(conn)
    first = repo.needing_analysis(None, 2)
    assert [c.audio_hash for c in first] == [A, B]
    second = repo.needing_analysis(first[-1].audio_hash, 2)
    assert [c.audio_hash for c in second] == [C, D]
    assert repo.needing_analysis(D, 2) == []


def test_needing_analysis_ignores_liner_only_audio(conn: psycopg.Connection[DictRow]) -> None:
    station_id, liner_id = uuid4(), uuid4()
    conn.execute("INSERT INTO stations (id, call_letters) VALUES (%s, %s)", (station_id, "KTST"))
    conn.execute(
        """INSERT INTO station_liners (id, station_id, name, file_name, format, audio_hash,
                                       duration_ms)
           VALUES (%s, %s, 'ID', %s, 'flac', %s, 5000)""",
        (liner_id, station_id, f"{liner_id}.flac", str(A)),
    )
    _cues(conn, A)
    assert _repo(conn).needing_analysis(None, 10) == []


def test_purge_other_versions(conn: psycopg.Connection[DictRow]) -> None:
    _song(conn, A)
    _song(conn, B)
    _repo(conn).store(A, SongEnding(EndingType.OTHER, None, 200_000))
    _repo(conn).store(B, SongEnding(EndingType.OTHER, None, 200_000))
    conn.execute("UPDATE stream_endings SET analyser_version = 99 WHERE audio_hash = %s", (str(B),))
    _repo(conn).purge_other_versions(ENDING_ANALYSER_VERSION)
    assert _repo(conn).get(A) is not None
    assert _repo(conn).get(B) is None


def test_prune_without_cues(conn: psycopg.Connection[DictRow]) -> None:
    _song(conn, A)
    _song(conn, B)
    _repo(conn).store(A, SongEnding(EndingType.OTHER, None, 200_000))
    _repo(conn).store(B, SongEnding(EndingType.OTHER, None, 200_000))
    conn.execute("DELETE FROM stream_cues WHERE audio_hash = %s", (str(B),))
    _repo(conn).prune_without_cues()
    assert _repo(conn).get(A) is not None
    assert _repo(conn).get(B) is None


def test_check_rejects_long_tail_without_eom(conn: psycopg.Connection[DictRow]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            """INSERT INTO stream_endings (audio_hash, ending, eom_ms, measured_cue_out_ms,
                                           analyser_version)
               VALUES (%s, 'long_tail', NULL, 200000, 1)""",
            (str(A),),
        )


def test_check_rejects_eom_on_other(conn: psycopg.Connection[DictRow]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            """INSERT INTO stream_endings (audio_hash, ending, eom_ms, measured_cue_out_ms,
                                           analyser_version)
               VALUES (%s, 'other', 190000, 200000, 1)""",
            (str(A),),
        )

"""Cue editor PR 1, Task 2: manual cue edits stored by audio hash in ``stream_cue_edits``.

Requirement R4 (edits are keyed by audio_hash, survive the analyser purge and the orphan
prune, and reset deletes them), plus the Task 2 Interfaces: the upsert/get/delete port, the
table's CHECK constraints, and ``StreamCueRepository.get``.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row
from structlog.testing import capture_logs

from backend.db.repositories.stream_cue_edits import PgStreamCueEditRepository
from backend.db.repositories.stream_cues import PgStreamCueRepository
from backend.domain.library import AudioHash
from backend.domain.streaming import CueAnalysis, CueMarkers, CuePoints

pytestmark = pytest.mark.integration

H = AudioHash.parse("flac-md5:" + "a" * 32)
H2 = AudioHash.parse("flac-md5:" + "b" * 32)
M = CueMarkers(start_ms=9_000, eom_ms=241_100, fade_at_ms=241_100, stop_ms=247_100)

HERETIC = CuePoints(
    cue_in_ms=0,
    cue_out_ms=247_100,
    fade_in_ms=0,
    fade_out_ms=6_000,
    start_next_ms=6_000,
    gain_db=-11.63,
)
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _analysis(
    audio_hash: AudioHash = H,
    *,
    cues: CuePoints = HERETIC,
    loudness_lufs: float | None = -14.5,
    analysis_failed: bool = False,
    analyser_version: int = 1,
) -> CueAnalysis:
    return CueAnalysis(
        audio_hash=audio_hash,
        cues=cues,
        loudness_lufs=loudness_lufs,
        analysis_failed=analysis_failed,
        analyser_version=analyser_version,
    )


@pytest.fixture
def conn(migrated_db: str) -> Generator[psycopg.Connection[DictRow]]:
    """An autocommit connection with an empty ``stream_cue_edits``.

    The shared ``migrated_db`` truncation does not know this table yet, so the fixture
    clears it before and after each test to keep tests independent of their order.
    """
    with psycopg.connect(migrated_db, autocommit=True, row_factory=dict_row) as c:
        c.execute("DELETE FROM stream_cue_edits")
        yield c
        c.execute("DELETE FROM stream_cue_edits")


def _count(conn: psycopg.Connection[Any], table: str, audio_hash: AudioHash) -> int:
    row = conn.execute(
        f"SELECT count(*) AS n FROM {table} WHERE audio_hash = %s",  # noqa: S608
        (str(audio_hash),),
    ).fetchone()
    assert row is not None
    return int(row["n"])


def test_save_then_get_round_trips(conn: psycopg.Connection[DictRow]):
    """R4: the four markers read back exactly as saved."""
    repo = PgStreamCueEditRepository(conn)

    repo.save(H, M)

    assert repo.get(H) == M


def test_save_twice_keeps_the_latest(conn: psycopg.Connection[DictRow]):
    """R4: a second save for the same audio replaces the first (one edit per audio)."""
    repo = PgStreamCueEditRepository(conn)
    later = CueMarkers(start_ms=9_000, eom_ms=241_100, fade_at_ms=241_100, stop_ms=246_000)

    repo.save(H, M)
    repo.save(H, later)

    assert repo.get(H) == later
    assert _count(conn, "stream_cue_edits", H) == 1


def test_save_stamps_edited_at_on_every_save(conn: psycopg.Connection[DictRow]):
    """Task 2 Interfaces: ``save`` is an upsert that sets ``edited_at = now()`` each time."""
    repo = PgStreamCueEditRepository(conn)
    repo.save(H, M)
    conn.execute("UPDATE stream_cue_edits SET edited_at = '2001-01-01T00:00:00Z'")

    repo.save(H, M)

    row = conn.execute("SELECT edited_at FROM stream_cue_edits").fetchone()
    assert row is not None
    assert row["edited_at"] > datetime(2001, 1, 1, tzinfo=UTC)


def test_edits_for_different_audio_are_independent(conn: psycopg.Connection[DictRow]):
    """R4: an edit follows its own audio only; a different version has its own edit."""
    repo = PgStreamCueEditRepository(conn)
    other = CueMarkers(start_ms=0, eom_ms=100_000, fade_at_ms=95_000, stop_ms=100_000)

    repo.save(H, M)
    repo.save(H2, other)

    assert repo.get(H) == M
    assert repo.get(H2) == other


def test_get_unknown_hash_is_none(conn: psycopg.Connection[DictRow]):
    """R4: audio with no edit has no markers, so the radio falls back to autocue."""
    assert PgStreamCueEditRepository(conn).get(H) is None


def test_delete_removes_and_is_idempotent(conn: psycopg.Connection[DictRow]):
    """R4: reset to automatic removes the edit; resetting again is not an error."""
    repo = PgStreamCueEditRepository(conn)
    repo.save(H, M)
    repo.save(H2, M)

    repo.delete(H)
    repo.delete(H)  # absent now: must not raise

    assert repo.get(H) is None
    assert repo.get(H2) == M  # only the named audio is removed


def test_delete_when_never_saved_is_not_an_error(conn: psycopg.Connection[DictRow]):
    """R4: deleting an edit that never existed is a no-op."""
    PgStreamCueEditRepository(conn).delete(H)


@pytest.mark.parametrize(
    ("start", "eom", "fade_at", "stop"),
    [
        (5_000, 5_000, 5_000, 10_000),  # eom = start
        (5_000, 4_000, 5_000, 10_000),  # eom < start
        (-1, 5_000, 5_000, 10_000),  # start < 0
        (0, 10_001, 5_000, 10_000),  # eom > stop
        (5_000, 8_000, 4_999, 10_000),  # fade_at < start
        (0, 8_000, 10_001, 10_000),  # fade_at > stop
    ],
)
def test_order_check_refuses_a_bad_row(
    conn: psycopg.Connection[DictRow], start: int, eom: int, fade_at: int, stop: int
):
    """R1: the table itself refuses a row that breaks the marker ordering."""
    with pytest.raises(psycopg.errors.CheckViolation) as raised:
        conn.execute(
            """INSERT INTO stream_cue_edits (audio_hash, start_ms, eom_ms, fade_at_ms, stop_ms)
               VALUES (%s, %s, %s, %s, %s)""",
            (str(H), start, eom, fade_at, stop),
        )

    assert raised.value.diag.constraint_name == "stream_cue_edits_order"


def test_order_check_accepts_the_boundary_orderings(conn: psycopg.Connection[DictRow]):
    """R1: EOM = Stop, Fade-out = Start and Fade-out = Stop are all legal rows."""
    repo = PgStreamCueEditRepository(conn)

    repo.save(H, CueMarkers(0, 10_000, 0, 10_000))
    repo.save(H2, CueMarkers(0, 5_000, 10_000, 10_000))

    assert repo.get(H) == CueMarkers(0, 10_000, 0, 10_000)
    assert repo.get(H2) == CueMarkers(0, 5_000, 10_000, 10_000)


def test_audio_hash_format_check_refuses_a_malformed_hash(conn: psycopg.Connection[DictRow]):
    """Task 2 Interfaces: ``audio_hash`` carries the same format CHECK as ``stream_cues``."""
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            """INSERT INTO stream_cue_edits (audio_hash, start_ms, eom_ms, fade_at_ms, stop_ms)
               VALUES ('not-a-hash', 0, 5000, 5000, 5000)"""
        )


def test_an_edit_needs_no_library_file(conn: psycopg.Connection[DictRow]):
    """R4 (D20): no foreign key; the edit is stored although no library file carries H."""
    count = conn.execute("SELECT count(*) AS n FROM library_files").fetchone()
    assert count is not None and count["n"] == 0

    PgStreamCueEditRepository(conn).save(H, M)

    assert PgStreamCueEditRepository(conn).get(H) == M


def test_purge_and_prune_leave_edits(conn: psycopg.Connection[DictRow]):
    """R4, anti-goal 1, Review Focus 2: neither autocue cleanup deletes a manual edit."""
    edits = PgStreamCueEditRepository(conn)
    cues = PgStreamCueRepository(conn)
    edits.save(H, M)
    edits.save(H2, M)

    # The orphan prune: no library file carries H.
    cues.upsert(_analysis(H))
    assert _count(conn, "stream_cues", H) == 1
    cues.prune_orphans(NOW, timedelta(0))  # first strike: mark
    cues.prune_orphans(NOW, timedelta(0))  # second strike: delete
    assert _count(conn, "stream_cues", H) == 0

    # The analyser purge: the analyser moved on from version 1.
    cues.upsert(_analysis(H2, analyser_version=1))
    assert _count(conn, "stream_cues", H2) == 1
    cues.purge_other_versions(2)
    assert _count(conn, "stream_cues", H2) == 0

    assert edits.get(H) == M
    assert edits.get(H2) == M


def test_stream_cues_get_returns_the_analysis(conn: psycopg.Connection[DictRow]):
    """Task 2 Interfaces: ``StreamCueRepository.get`` returns the stored CueAnalysis."""
    cues = PgStreamCueRepository(conn)
    analysis = _analysis(H)

    cues.upsert(analysis)

    assert cues.get(H) == analysis


def test_stream_cues_get_returns_a_failed_analysis_as_stored(conn: psycopg.Connection[DictRow]):
    """Task 2 Interfaces (D52): a fallback row keeps its flag, its gain and a null loudness."""
    cues = PgStreamCueRepository(conn)
    fallback = _analysis(
        H,
        cues=CuePoints(
            cue_in_ms=0,
            cue_out_ms=200_000,
            fade_in_ms=0,
            fade_out_ms=4_000,
            start_next_ms=0,
            gain_db=-8.0,
        ),
        loudness_lufs=None,
        analysis_failed=True,
    )

    cues.upsert(fallback)

    assert cues.get(H) == fallback


def test_stream_cues_get_unknown_is_none(conn: psycopg.Connection[DictRow]):
    """Task 2 Interfaces: no row, no analysis."""
    assert PgStreamCueRepository(conn).get(H) is None


def test_stream_cues_get_ignores_other_audio(conn: psycopg.Connection[DictRow]):
    """Task 2 Interfaces: ``get`` answers for the asked audio only."""
    cues = PgStreamCueRepository(conn)
    cues.upsert(_analysis(H2))

    assert cues.get(H) is None


def test_stream_cues_get_returns_none_and_warns_for_an_invalid_row(
    conn: psycopg.Connection[DictRow],
):
    """Task 2 Interfaces: a stored row that fails validation reads as none, logged."""
    conn.execute(
        """INSERT INTO stream_cues (audio_hash, cue_in_ms, cue_out_ms, fade_in_ms, fade_out_ms,
                                    start_next_ms, gain_db, analyser_version)
           VALUES (%s, 5000, 1000, 0, 0, 0, 0.0, 1)""",
        (str(H),),
    )

    with capture_logs() as logs:
        found = PgStreamCueRepository(conn).get(H)

    assert found is None
    invalid = [e for e in logs if e["event"] == "stream_cue_row_invalid"]
    assert [e["log_level"] for e in invalid] == ["warning"]

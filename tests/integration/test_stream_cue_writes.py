"""The cue store's writes against PostgreSQL.

Spec: For PR D and PR E ("reads the hash before analysing and stores nothing if the hash
changed meanwhile"); Data ("analysed_at ... stamped by every upsert"); D20 ("an analyser change
purges its old rows once"); D56 (two-strike prune: marked, deleted at least 24 h later if still
orphaned, mark cleared if the hash reappears; a missing file is still in the library);
D66 (the mark is the nullable column stream_cues.orphaned_at, and an upsert clears it).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.stream_cues import PgStreamCueRepository
from backend.domain.library import AudioHash
from backend.domain.streaming import CUE_ANALYSER_VERSION, CueAnalysis, CuePoints
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import Conn

T0 = datetime(2026, 9, 30, 4, 0, tzinfo=UTC)
GRACE = timedelta(hours=24)
POINTS = CuePoints(
    cue_in_ms=400,
    cue_out_ms=181_200,
    fade_in_ms=200,
    fade_out_ms=2_100,
    start_next_ms=2_100,
    gain_db=-11.907,
)


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def analysis(
    audio: str, *, version: int = CUE_ANALYSER_VERSION, failed: bool = False
) -> CueAnalysis:
    return CueAnalysis(
        audio_hash=AudioHash.parse(audio),
        cues=POINTS,
        loudness_lufs=None,
        analysis_failed=failed,
        analyser_version=version,
    )


def hashed_file(conn: Conn, *, status: str = "present") -> tuple[UUID, str]:
    audio = seed.audio_hash()
    return seed.library_file(conn, audio_hash=audio, status=status), audio


def cue_rows(conn: Conn) -> dict[str, dict[str, object]]:
    rows = conn.execute("SELECT * FROM stream_cues").fetchall()
    return {str(r["audio_hash"]): dict(r) for r in rows}


def marks(conn: Conn) -> dict[str, datetime]:
    rows = conn.execute(
        "SELECT audio_hash, orphaned_at FROM stream_cues WHERE orphaned_at IS NOT NULL"
    ).fetchall()
    return {str(r["audio_hash"]): r["orphaned_at"] for r in rows}


def orphan_row(conn: Conn) -> str:
    """A current cue row whose audio no library file carries."""
    file_id, audio = hashed_file(conn)
    PgStreamCueRepository(conn).store_if_current(analysis(audio), file_id)
    conn.execute("DELETE FROM library_files WHERE id = %s", (file_id,))
    return audio


def test_an_analysis_is_stored_while_the_file_still_has_the_hash(conn: Conn) -> None:
    """Keyed by the hash read before analysing; analysed_at stamped (Data)."""
    file_id, audio = hashed_file(conn)
    stored = PgStreamCueRepository(conn).store_if_current(analysis(audio), file_id)
    row = cue_rows(conn)[audio]
    assert stored is True
    fields = (
        "cue_in_ms",
        "cue_out_ms",
        "fade_in_ms",
        "fade_out_ms",
        "start_next_ms",
        "loudness_lufs",
        "analyser_version",
        "analysis_failed",
    )
    assert {k: row[k] for k in fields} == {
        "cue_in_ms": 400,
        "cue_out_ms": 181_200,
        "fade_in_ms": 200,
        "fade_out_ms": 2_100,
        "start_next_ms": 2_100,
        "loudness_lufs": None,
        "analyser_version": CUE_ANALYSER_VERSION,
        "analysis_failed": False,
    }
    assert row["gain_db"] == pytest.approx(-11.907, abs=1e-3)
    assert row["analysed_at"] is not None


@pytest.mark.parametrize("change", ["another hash", "hash cleared", "file deleted"])
def test_nothing_is_stored_once_the_hash_changed(conn: Conn, change: str) -> None:
    """For PR D and PR E: "stores nothing if the hash changed meanwhile" (a rescan, a retag
    that clears the hash until the backfill recomputes it, or the row's deletion)."""
    file_id, audio = hashed_file(conn)
    if change == "another hash":
        conn.execute(
            "UPDATE library_files SET audio_hash = %s WHERE id = %s", (seed.audio_hash(), file_id)
        )
    elif change == "hash cleared":
        conn.execute("UPDATE library_files SET audio_hash = NULL WHERE id = %s", (file_id,))
    else:
        conn.execute("DELETE FROM library_files WHERE id = %s", (file_id,))
    stored = PgStreamCueRepository(conn).store_if_current(analysis(audio), file_id)
    assert (stored, cue_rows(conn)) == (False, {})


def test_a_new_analysis_replaces_the_earlier_row(conn: Conn) -> None:
    """Data: one row per audio; a store replaces it."""
    file_id, audio = hashed_file(conn)
    store = PgStreamCueRepository(conn)
    store.store_if_current(analysis(audio, failed=True), file_id)
    store.store_if_current(analysis(audio), file_id)
    rows = cue_rows(conn)
    assert list(rows) == [audio]
    assert rows[audio]["analysis_failed"] is False


def test_purging_drops_rows_of_other_versions_and_their_marks(conn: Conn) -> None:
    """D20: "an analyser change purges its old rows once"; a second purge changes nothing."""
    old_file, old = hashed_file(conn)
    current_file, current = hashed_file(conn)
    store = PgStreamCueRepository(conn)
    store.store_if_current(analysis(old, version=CUE_ANALYSER_VERSION + 1), old_file)
    store.store_if_current(analysis(current), current_file)
    conn.execute("UPDATE stream_cues SET orphaned_at = %s WHERE audio_hash = %s", (T0, old))
    store.purge_other_versions(CUE_ANALYSER_VERSION)
    store.purge_other_versions(CUE_ANALYSER_VERSION)
    assert (set(cue_rows(conn)), marks(conn)) == ({current}, {})


def test_the_first_prune_marks_an_orphan_and_keeps_it(conn: Conn) -> None:
    """D56 first strike: marked orphaned_at, not deleted."""
    audio = orphan_row(conn)
    PgStreamCueRepository(conn).prune_orphans(T0, GRACE)
    assert (set(cue_rows(conn)), marks(conn)) == ({audio}, {audio: T0})


def test_an_orphan_marked_less_than_24_hours_ago_is_kept(conn: Conn) -> None:
    """D56: "at least 24 h later"; the first mark's time is kept."""
    audio = orphan_row(conn)
    store = PgStreamCueRepository(conn)
    store.prune_orphans(T0, GRACE)
    store.prune_orphans(T0 + timedelta(hours=23), GRACE)
    assert (set(cue_rows(conn)), marks(conn)) == ({audio}, {audio: T0})


def test_an_orphan_still_orphaned_24_hours_later_is_deleted(conn: Conn) -> None:
    """D56 second strike: deleted, with its mark."""
    orphan_row(conn)
    store = PgStreamCueRepository(conn)
    store.prune_orphans(T0, GRACE)
    store.prune_orphans(T0 + GRACE, GRACE)
    assert (cue_rows(conn), marks(conn)) == ({}, {})


def test_prune_clears_the_mark_when_the_hash_reappears(conn: Conn) -> None:
    """D56: a retagged MP3 re-hashed by the backfill keeps its cues."""
    audio = orphan_row(conn)
    store = PgStreamCueRepository(conn)
    store.prune_orphans(T0, GRACE)
    seed.library_file(conn, audio_hash=audio)
    store.prune_orphans(T0 + GRACE, GRACE)
    assert (set(cue_rows(conn)), marks(conn)) == ({audio}, {})


def test_audio_a_missing_file_still_carries_is_not_an_orphan(conn: Conn) -> None:
    """D20 "no longer in the library": a missing file is still in the library (it may come
    back or be remapped)."""
    file_id, audio = hashed_file(conn, status="missing")
    store = PgStreamCueRepository(conn)
    store.store_if_current(analysis(audio), file_id)
    store.prune_orphans(T0, GRACE)
    store.prune_orphans(T0 + GRACE, GRACE)
    assert (set(cue_rows(conn)), marks(conn)) == ({audio}, {})


def test_a_store_clears_the_orphan_mark(conn: Conn) -> None:
    """D66: "An upsert clears orphaned_at": audio analysed again is in the library."""
    file_id, audio = hashed_file(conn)
    store = PgStreamCueRepository(conn)
    store.store_if_current(analysis(audio), file_id)
    conn.execute("UPDATE stream_cues SET orphaned_at = %s", (T0,))
    store.store_if_current(analysis(audio, failed=True), file_id)
    assert marks(conn) == {}


def test_an_upsert_clears_the_orphan_mark(conn: Conn) -> None:
    """D66: "An upsert clears orphaned_at" (the PR C write, kept for its locked tests, N3)."""
    file_id, audio = hashed_file(conn)
    store = PgStreamCueRepository(conn)
    store.store_if_current(analysis(audio), file_id)
    conn.execute("UPDATE stream_cues SET orphaned_at = %s", (T0,))
    store.upsert(analysis(audio))
    assert marks(conn) == {}

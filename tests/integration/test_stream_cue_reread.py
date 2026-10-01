"""The per-song re-read of stored cues, against PostgreSQL (spec: D85, "Cues are re-read for
each song just before it is queued"; D20, cues belong to the audio: the file's audio_hash
looks up stream_cues, no row or no hash means no cues, a row that fails validation means no
cues, logged; the D85 brief: an indexed read)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import cast

import psycopg
import pytest
from psycopg.rows import dict_row
from structlog.testing import capture_logs

from backend.db.repositories.playable_schedule import PgPlayableScheduleRepository
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import Conn
from tests.integration.test_playable_schedule_fence import StatementRecorder, plan_of, walk

pytestmark = pytest.mark.integration


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def test_the_stored_row_of_the_files_audio_is_read(conn: Conn) -> None:
    """D20: the row is the audio's, so a bit-identical twin reads the same cues."""
    file_id, audio = seed.cued_file(conn)
    twin = seed.library_file(conn, audio_hash=audio)
    reader = PgPlayableScheduleRepository(conn)
    assert [reader.file_cues(f) for f in (file_id, twin)] == [seed.CUE_POINTS, seed.CUE_POINTS]


def test_no_row_means_no_cues(conn: Conn) -> None:
    file_id = seed.library_file(conn, audio_hash=seed.audio_hash())
    assert PgPlayableScheduleRepository(conn).file_cues(file_id) is None


def test_no_audio_hash_means_no_cues(conn: Conn) -> None:
    """D20: "A file with no audio_hash has no cues", for example an MP3 whose hash a retag
    cleared until the backfill recomputes it."""
    file_id = seed.library_file(conn)
    assert PgPlayableScheduleRepository(conn).file_cues(file_id) is None


def test_an_invalid_row_means_no_cues_and_is_logged(conn: Conn) -> None:
    """As the day read: a row that fails CuePoints validation is logged, never raised. Review
    M5: warned once per audio per app run, then debug, so a bad row does not warn at every
    play of its song, nor again for a twin that shares the audio (D20; audit N8)."""
    audio = seed.audio_hash()
    file_id = seed.library_file(conn, audio_hash=audio)
    twin = seed.library_file(conn, audio_hash=audio)
    seed.cue_row(conn, audio, fade_in_ms=-1)
    reader = PgPlayableScheduleRepository(conn)
    with capture_logs() as logs:
        answers = [reader.file_cues(f) for f in (file_id, file_id, twin)]
    assert answers == [None, None, None]
    events = [e for e in logs if e["event"] == "stream_cue_reread_invalid"]
    assert [(e["log_level"], e["file_id"]) for e in events] == [
        ("warning", str(file_id)),
        ("debug", str(file_id)),
        ("debug", str(twin)),
    ]
    assert "fade_in_ms" in str(events[0]["error"])


LOOKED_UP_BY = {
    "library_files": re.compile(r"\bid = "),
    "stream_cues": re.compile(r"\baudio_hash = "),
}
"""How each table must be reached: ``library_files`` by its id, ``stream_cues`` by its
``audio_hash`` (both primary keys)."""


def test_the_read_is_served_by_indexes(conn: Conn) -> None:
    """The D85 brief: an indexed read. Every statement the re-read runs is planned with
    sequential scans priced out; no Seq Scan may appear, and each table is looked up by its
    key in an index condition, not scanned through an index with a filter (audit SF-4).
    The number of statements is not pinned."""
    file_id, _ = seed.cued_file(conn)
    recorder = StatementRecorder(conn)
    PgPlayableScheduleRepository(cast(Conn, recorder)).file_cues(file_id)
    assert recorder.statements, "the re-read ran no statement"
    conn.execute("SET enable_seqscan = off")
    plans = [plan_of(conn, query, params) for query, params in recorder.statements]
    nodes = [n for plan in plans for n in walk(plan)]
    relations = {str(n["Relation Name"]) for n in nodes if "Relation Name" in n}
    assert relations == {"library_files", "stream_cues"}
    assert [n["Node Type"] for n in nodes if n["Node Type"] == "Seq Scan"] == []
    for node in nodes:
        key = LOOKED_UP_BY.get(str(node.get("Relation Name")))
        if key is not None:
            conditions = [str(node.get(c, "")) for c in ("Index Cond", "Recheck Cond")]
            assert any(key.search(c) for c in conditions), node

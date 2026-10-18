"""Station liners, PR L2 Task 9: the stream reads a station's playable liners from PostgreSQL
(``PgPlayableLinerRepository``, one statement: the liners LEFT JOIN ``stream_cues`` by hash),
and the composition root wires that reader into the stream's repositories.

Requirements: LR4 (a liner plays with ``liner_points``: before analysis the whole file with EOM
at the end; an autocue row's points and gain, never a fade; a manual edit's positions at the
row's gain; a silent liner never plays), LR3 (stored per station). Order is not specified, so
lists are compared by liner id.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.config import Settings
from backend.db.repositories.station_liners import (
    PgPlayableLinerRepository,
    PgStationLinerRepository,
)
from backend.db.repositories.stream_cues import PgStreamCueRepository
from backend.domain.library import AudioHash
from backend.domain.liners import PlayableLiner, StationLiner, liner_markers
from backend.domain.streaming import ClipFormat, CueAnalysis, CuePoints
from backend.services.streaming.service import StreamRepos
from tests.integration import stream_seed as seed
from tests.services.streaming.helpers import EngineStarts

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
ROW = CuePoints(120, 6_900, 500, 400, 300, -3.5)
"""Autocue's row, with a fade-in and a fade-out a liner must never play."""


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, autocommit=True, row_factory=dict_row) as c:
        yield c


def make_liner(station_id: UUID, *, duration_ms: int = 7_000) -> StationLiner:
    liner_id = uuid4()
    return StationLiner(
        id=liner_id,
        station_id=station_id,
        name="WKRP ID",
        file_name=f"{liner_id}.flac",
        format=ClipFormat.FLAC,
        audio_hash=AudioHash.parse(f"flac-md5:{uuid4().hex}"),
        duration_ms=duration_ms,
        markers=None,
        uploaded_at=T0,
    )


def analysis(audio: AudioHash, *, failed: bool = False, silent: bool = False) -> CueAnalysis:
    return CueAnalysis(
        audio_hash=audio,
        cues=ROW,
        loudness_lufs=None,
        analysis_failed=failed,
        analyser_version=1,
        audio_silent=silent,
    )


def by_id(liners: list[PlayableLiner]) -> dict[UUID, PlayableLiner]:
    found = {liner.liner_id: liner for liner in liners}
    assert len(found) == len(liners), "a liner is listed twice"
    return found


def test_playable_uses_liner_points(conn: Conn) -> None:
    """LR4, plan note 6: no row → the whole file at the no-cues gain; a row, measured or failed
    → its points with no fades; an edit → its positions at the row's gain, fade 0; a silent
    liner is absent, edited or not (silent wins)."""
    repo = PgStationLinerRepository(conn)
    cues = PgStreamCueRepository(conn)
    station_id = seed.station(conn)
    unanalysed = make_liner(station_id, duration_ms=8_250)
    analysed = make_liner(station_id)
    edited = make_liner(station_id)
    failed = make_liner(station_id)
    silent = make_liner(station_id)
    edited_silent = make_liner(station_id)
    for liner in (unanalysed, analysed, edited, failed, silent, edited_silent):
        repo.add(liner)
    cues.upsert(analysis(analysed.audio_hash))
    cues.upsert(analysis(edited.audio_hash))
    cues.upsert(analysis(failed.audio_hash, failed=True))
    cues.upsert(analysis(silent.audio_hash, failed=True, silent=True))
    cues.upsert(analysis(edited_silent.audio_hash, failed=True, silent=True))
    repo.save_markers(edited.id, liner_markers(100, 5_000, 6_500))
    repo.save_markers(edited_silent.id, liner_markers(100, 5_000, 6_500))

    playable = by_id(PgPlayableLinerRepository(conn).playable(station_id))

    assert playable == {
        unanalysed.id: PlayableLiner(
            unanalysed.id, unanalysed.file_name, CuePoints(0, 8_250, 0, 0, 0, 0.0)
        ),
        analysed.id: PlayableLiner(
            analysed.id, analysed.file_name, CuePoints(120, 6_900, 0, 0, 300, -3.5)
        ),
        edited.id: PlayableLiner(
            edited.id, edited.file_name, CuePoints(100, 6_500, 0, 0, 1_500, -3.5)
        ),
        failed.id: PlayableLiner(
            failed.id, failed.file_name, CuePoints(120, 6_900, 0, 0, 300, -3.5)
        ),
    }


def test_playable_is_per_station(conn: Conn) -> None:
    """LR3: only the station's own liners; a station with none (or no such station) has an
    empty list."""
    repo = PgStationLinerRepository(conn)
    mine, other, empty = seed.station(conn), seed.station(conn), seed.station(conn)
    own = make_liner(mine)
    theirs = make_liner(other)
    repo.add(own)
    repo.add(theirs)
    reader = PgPlayableLinerRepository(conn)

    assert list(by_id(reader.playable(mine))) == [own.id]
    assert list(by_id(reader.playable(other))) == [theirs.id]
    assert reader.playable(empty) == []
    assert reader.playable(uuid4()) == []


def test_main_wires_the_liner_reader(conn: Conn, migrated_db: str, tmp_path: Path) -> None:
    """Plan Task 9: the stream's repositories, as the composition root opens them
    (``main.build_stream_ports``), carry the PostgreSQL liner reader."""
    from backend.main import build_stream_ports

    station_id = seed.station(conn)
    liner = make_liner(station_id)
    PgStationLinerRepository(conn).add(liner)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, database_url=migrated_db, stream_work_dir=tmp_path / "work"
    )
    ports = build_stream_ports(settings, EngineStarts())

    opened: AbstractContextManager[StreamRepos] = ports.repos()
    with opened as repos:
        assert isinstance(repos.liners, PgPlayableLinerRepository)
        assert [x.liner_id for x in repos.liners.playable(station_id)] == [liner.id]

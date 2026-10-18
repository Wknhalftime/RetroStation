"""Station liners, PR L1 Task 2 fix round 1: ``PgStationLinerRepository`` applies the
``StationLiner`` rules on rename and save_markers, as ``FakeStationLiners`` does, so a value
the domain refuses raises ``InvalidLinerValueError`` and never reaches the table.

Requirements: LR3 (rename changes only the name), LR4 (a liner's edit: fade at Stop, Stop
within the duration).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.db.repositories.station_liners import PgStationLinerRepository
from backend.domain.library import AudioHash
from backend.domain.liners import InvalidLinerValueError, StationLiner, liner_markers
from backend.domain.streaming import ClipFormat, CueMarkers
from tests.integration import stream_seed as seed

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, autocommit=True, row_factory=dict_row) as c:
        yield c


def stored_liner(conn: Conn) -> StationLiner:
    liner_id = uuid4()
    liner = StationLiner(
        id=liner_id,
        station_id=seed.station(conn),
        name="WKRP ID",
        file_name=f"{liner_id}.flac",
        format=ClipFormat.FLAC,
        audio_hash=AudioHash.parse(f"flac-md5:{uuid4().hex}"),
        duration_ms=7_000,
        markers=None,
        uploaded_at=T0,
    )
    PgStationLinerRepository(conn).add(liner)
    return liner


def test_a_padded_rename_is_stored_stripped(conn: Conn) -> None:
    """LR3: the name is stored as the domain normalises it."""
    repo = PgStationLinerRepository(conn)
    liner = stored_liner(conn)

    repo.rename(liner.id, "  Legal ID  ")

    found = conn.execute("SELECT name FROM station_liners WHERE id = %s", (liner.id,))
    assert found.fetchone() == {"name": "Legal ID"}
    assert repo.get(liner.id) == replace(liner, name="Legal ID")


@pytest.mark.parametrize("name", ["   ", "x" * 256], ids=["blank", "256 chars"])
def test_an_invalid_rename_is_refused_and_the_list_still_reads(conn: Conn, name: str) -> None:
    """LR3: a name the domain refuses raises the domain error; the row keeps its old name,
    so the station's list still reads."""
    repo = PgStationLinerRepository(conn)
    liner = stored_liner(conn)

    with pytest.raises(InvalidLinerValueError):
        repo.rename(liner.id, name)

    assert repo.get(liner.id) == liner
    assert [e.liner for e in repo.list_for_station(liner.station_id)] == [liner]


@pytest.mark.parametrize(
    "markers",
    [
        liner_markers(0, 5_000, 7_001),  # Stop past the 7,000 ms duration
        CueMarkers(0, 5_000, 6_000, 7_000),  # the fade not at Stop
    ],
    ids=["stop past duration", "fade not at stop"],
)
def test_markers_a_liner_refuses_raise_the_domain_error(conn: Conn, markers: CueMarkers) -> None:
    """LR4: an edit the domain refuses raises ``InvalidLinerValueError`` (never a psycopg
    error), and the liner keeps no edit."""
    repo = PgStationLinerRepository(conn)
    liner = stored_liner(conn)

    with pytest.raises(InvalidLinerValueError):
        repo.save_markers(liner.id, markers)

    assert repo.get(liner.id) == liner


def test_rename_and_markers_of_an_unknown_liner_do_nothing(conn: Conn) -> None:
    """LR3: as the port says, an unknown id is not an error and writes nothing."""
    repo = PgStationLinerRepository(conn)
    liner = stored_liner(conn)

    repo.rename(uuid4(), "Legal ID")
    repo.save_markers(uuid4(), liner_markers(0, 5_000, 7_000))

    assert repo.get(liner.id) == liner

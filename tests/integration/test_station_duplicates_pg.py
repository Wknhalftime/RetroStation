"""PG: the station adapter turns a duplicate call sign into the domain's error (spec D72; C1:
low-level errors are handled at their layer, so routes catch domain exceptions only;
the translation is keyed by constraint, so another unique violation is
not called a duplicate; it may surface as the driver's error or as another broadcast
error)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.domain.broadcast import (
    BroadcastError,
    BroadcastStation,
    DuplicateCallLettersError,
)


@pytest.fixture
def stations(migrated_db: str) -> Iterator[PgBroadcastStationRepository]:
    conn = psycopg.connect(migrated_db, row_factory=dict_row)
    try:
        yield PgBroadcastStationRepository(conn)
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize("twin", ["KAZR-FM", "kazr-fm"], ids=["exact", "case"])
def test_creating_a_twin_raises_duplicate_call_letters(
    stations: PgBroadcastStationRepository, twin: str
) -> None:
    stations.create(BroadcastStation(id=uuid4(), call_letters="KAZR-FM"))
    with pytest.raises(DuplicateCallLettersError):
        stations.create(BroadcastStation(id=uuid4(), call_letters=twin))


@pytest.mark.parametrize("twin", ["KAZR-FM", "kazr-fm"], ids=["exact", "case"])
def test_renaming_onto_a_twin_raises_duplicate_call_letters(
    stations: PgBroadcastStationRepository, twin: str
) -> None:
    stations.create(BroadcastStation(id=uuid4(), call_letters="KAZR-FM"))
    kioa = stations.create(BroadcastStation(id=uuid4(), call_letters="KIOA-FM"))
    with pytest.raises(DuplicateCallLettersError):
        stations.update(replace(kioa, call_letters=twin))


def test_another_unique_violation_is_not_called_a_duplicate(
    stations: PgBroadcastStationRepository,
) -> None:
    kazr = stations.create(BroadcastStation(id=uuid4(), call_letters="KAZR-FM"))
    with pytest.raises((psycopg.errors.UniqueViolation, BroadcastError)) as raised:
        stations.create(BroadcastStation(id=kazr.id, call_letters="KIOA-FM"))  # same id
    assert not isinstance(raised.value, DuplicateCallLettersError)

"""The not-an-artist list's port, its fake against its PostgreSQL adapter (artist model phase
1c, SPEC S6; C7).

``LogNamePlaceholderRepository`` holds the log names that are not artists, by their exact
cleaned name (``normalized_name``): migration 0045 seeded the five station names, and the
curator adds and removes names with Not an artist (S6, S9).

- ``listed(names)``: which of *names* are on the list; exact names only, so "38 special" and
  "spot breaks" are not "special programming" or "spot break".
- ``add(name, added_by)``: puts the name on the list; whether it was added (a name already
  listed keeps its row, so pressing Not an artist twice is one row, E4).
- ``remove(name)``: takes it off; whether it was listed.

On PostgreSQL the port writes in the caller's transaction and commits nothing (C75); each case
is rolled back, so the five seeded names stay for the other tests.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.enums import PlaceholderSource
from backend.repositories.log_name_placeholders import LogNamePlaceholderRepository
from backend.services.repository_factory import RepositoryFactory
from tests.fakes.log_name_placeholders import FakeLogNamePlaceholderRepository

STATION_PLACEHOLDERS = {
    "spot break",
    "special programming",
    "syndicated program",
    "various",
    "various artists",
}
NEW_NAME = "zz contract traffic report"


@pytest.fixture(params=["fake", pytest.param("pg", marks=pytest.mark.integration)])
def placeholders(request: pytest.FixtureRequest) -> Iterator[LogNamePlaceholderRepository]:
    if request.param == "fake":
        yield FakeLogNamePlaceholderRepository(STATION_PLACEHOLDERS)
        return
    migrated_db: str = request.getfixturevalue("migrated_db")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        yield RepositoryFactory(conn).log_name_placeholders
        conn.rollback()


def test_the_five_station_names_are_listed(placeholders: LogNamePlaceholderRepository) -> None:
    assert placeholders.listed(STATION_PLACEHOLDERS | {"metallica"}) == STATION_PLACEHOLDERS


def test_only_exact_names_are_listed(placeholders: LogNamePlaceholderRepository) -> None:
    near = {"38 special", "spot breaks", "spot", "the various", "various artists live", "Various"}

    assert placeholders.listed(near) == set()


def test_nothing_asked_is_nothing_listed(placeholders: LogNamePlaceholderRepository) -> None:
    assert placeholders.listed(set()) == set()


def test_add_lists_a_name_once(placeholders: LogNamePlaceholderRepository) -> None:
    assert placeholders.add(NEW_NAME, PlaceholderSource.CURATOR) is True
    assert placeholders.add(NEW_NAME, PlaceholderSource.CURATOR) is False

    assert placeholders.listed({NEW_NAME, "spot break"}) == {NEW_NAME, "spot break"}


def test_adding_a_listed_seed_name_adds_nothing(
    placeholders: LogNamePlaceholderRepository,
) -> None:
    assert placeholders.add("spot break", PlaceholderSource.CURATOR) is False
    assert placeholders.listed({"spot break"}) == {"spot break"}


def test_remove_takes_a_name_off(placeholders: LogNamePlaceholderRepository) -> None:
    placeholders.add(NEW_NAME, PlaceholderSource.CURATOR)

    assert placeholders.remove(NEW_NAME) is True
    assert placeholders.remove(NEW_NAME) is False
    assert placeholders.listed({NEW_NAME}) == set()


def test_removing_a_name_never_listed_is_false(
    placeholders: LogNamePlaceholderRepository,
) -> None:
    assert placeholders.remove("metallica") is False
    assert placeholders.listed(STATION_PLACEHOLDERS) == STATION_PLACEHOLDERS


def test_a_seed_name_can_come_off_and_back(placeholders: LogNamePlaceholderRepository) -> None:
    assert placeholders.remove("various") is True
    assert placeholders.listed({"various", "various artists"}) == {"various artists"}

    assert placeholders.add("various", PlaceholderSource.CURATOR) is True
    assert placeholders.listed({"various"}) == {"various"}


@pytest.mark.integration
def test_the_adapter_writes_in_the_callers_transaction(migrated_db: str) -> None:
    """C75: the adapter commits nothing. Another connection sees neither write until the
    caller commits (this one rolls back)."""
    with (
        psycopg.connect(migrated_db, row_factory=dict_row) as conn,
        psycopg.connect(migrated_db, row_factory=dict_row, autocommit=True) as other,
    ):
        placeholders = RepositoryFactory(conn).log_name_placeholders
        placeholders.add(NEW_NAME, PlaceholderSource.CURATOR)
        placeholders.remove("various")

        rows = other.execute("SELECT normalized_name FROM log_name_placeholders").fetchall()
        seen = {row["normalized_name"] for row in rows}
        conn.rollback()

    assert NEW_NAME not in seen
    assert "various" in seen

"""Integration tests: ``PgPlayFileResolutionRepository.get_for_plays`` (spec D16, D17, D21).

The repository reads curation's view ``play_file_resolution`` for the plays it is given and
adds no rules of its own: each play maps to the view's final ``file_id`` and ``file_status``,
or to no file. The view reports a final file that is not present with its status; the
consumer decides what is playable (D21).
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.play_file_resolution import PgPlayFileResolutionRepository
from backend.domain.curation import PlayFileResolution
from backend.domain.enums import FileStatus
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import Conn, at


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def logged(conn: Conn, matched: UUID) -> UUID:
    """A play on a fresh station whose one match is ``matched``."""
    playlist = seed.playlist(conn, seed.station(conn))
    return seed.matched_play(conn, playlist, at("08:00"), matched)


class TestGetForPlays:
    def test_a_resolved_play_maps_to_its_final_file_and_status(self, conn: Conn) -> None:
        master = seed.mastered_file(conn)
        event = logged(conn, master)

        result = PgPlayFileResolutionRepository(conn).get_for_plays([event])

        assert result == {event: PlayFileResolution(event, master, FileStatus.PRESENT)}

    def test_a_final_file_that_is_not_present_keeps_its_status(self, conn: Conn) -> None:
        master = seed.mastered_file(conn, status="deleted")
        event = logged(conn, master)

        result = PgPlayFileResolutionRepository(conn).get_for_plays([event])

        assert result == {event: PlayFileResolution(event, master, FileStatus.DELETED)}

    def test_a_play_with_no_final_file_maps_to_no_file(self, conn: Conn) -> None:
        """D22: a matched file whose work has no override or master resolves to nothing."""
        event = logged(conn, seed.library_file(conn))

        result = PgPlayFileResolutionRepository(conn).get_for_plays([event])

        assert result == {event: PlayFileResolution(event, None, None)}

    def test_each_requested_play_is_answered(self, conn: Conn) -> None:
        """A caller looks each of its plays up by id, so every id it asks for is a key; an
        id the view has no row for maps to no file."""
        resolved = logged(conn, seed.mastered_file(conn))
        unknown = uuid4()

        result = PgPlayFileResolutionRepository(conn).get_for_plays([resolved, unknown])

        assert set(result) == {resolved, unknown}
        assert result[unknown] == PlayFileResolution(unknown, None, None)

    def test_no_plays_reads_nothing(self, conn: Conn) -> None:
        assert PgPlayFileResolutionRepository(conn).get_for_plays([]) == {}

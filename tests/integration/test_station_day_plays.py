"""Acceptance tests: the broadcast view ``station_day_plays`` (spec D3, D4, D17's pattern).

The single definition of "a station's plays on a date". Columns:

- ``play_event_id``, ``station_id``, ``identity_id``: which play, where, of what;
- ``play_date``: the station day, the date of ``played_at`` read ``AT TIME ZONE 'UTC'``
  (``played_at`` is station wall-clock under a UTC label, D3), whatever the session TimeZone;
- ``logged_at``: that wall-clock time, naive;
- ``position``: the play's 0-based index in its station-day under the D4 order
  ``(played_at, identity_id, event_id)``. A view has no row order, so the order is a column.

Every logged play appears once; the view does no file resolution.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.integration import stream_seed as seed
from tests.integration.stream_seed import DAY, Conn, at, wall

PREV = DAY - timedelta(days=1)
NEXT = DAY + timedelta(days=1)

COLUMNS = ("play_event_id", "station_id", "play_date", "logged_at", "identity_id", "position")


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def day_rows(conn: Conn, station_id: UUID, day: date = DAY) -> list[dict[str, Any]]:
    """The station-day, read the way a consumer does: filter by column, order by position."""
    return conn.execute(
        """SELECT play_event_id, station_id, play_date, logged_at, identity_id, position
           FROM station_day_plays
           WHERE station_id = %s AND play_date = %s
           ORDER BY position""",
        (station_id, day),
    ).fetchall()


def events(conn: Conn, station_id: UUID, day: date = DAY) -> list[UUID]:
    return [UUID(str(r["play_event_id"])) for r in day_rows(conn, station_id, day)]


def set_zone(conn: Conn, zone: str) -> None:
    conn.execute("SELECT set_config('TimeZone', %s, false)", (zone,))
    shown = conn.execute("SHOW TimeZone").fetchone()
    assert shown is not None
    assert shown["TimeZone"] == zone


# --- columns ---------------------------------------------------------------------------------


class TestColumns:
    def test_a_play_maps_every_column(self, conn: Conn) -> None:
        st = seed.station(conn)
        ident = seed.identity(conn)
        event = seed.play(conn, seed.playlist(conn, st), ident, at("07:15:42"))

        [row] = day_rows(conn, st)

        assert (
            UUID(str(row["play_event_id"])),
            UUID(str(row["station_id"])),
            row["play_date"],
            row["logged_at"],
            UUID(str(row["identity_id"])),
            row["position"],
        ) == (event, st, DAY, wall("07:15:42"), ident, 0)

    def test_logged_at_is_naive_wall_clock(self, conn: Conn) -> None:
        st = seed.station(conn)
        seed.play(conn, seed.playlist(conn, st), seed.identity(conn), at("23:30"))
        set_zone(conn, "America/Chicago")

        [row] = day_rows(conn, st)

        assert isinstance(row["logged_at"], datetime)
        assert row["logged_at"].tzinfo is None
        assert row["logged_at"] == wall("23:30")

    def test_the_view_carries_no_file_resolution(self, conn: Conn) -> None:
        columns = {
            r["column_name"]
            for r in conn.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'station_day_plays'"""
            ).fetchall()
        }

        assert set(COLUMNS) <= columns
        assert not columns & {"file_id", "library_file_id", "file_status", "source"}


# --- the day window (D3) ---------------------------------------------------------------------


class TestWindow:
    @pytest.mark.parametrize("zone", ["America/Chicago", "UTC"])
    def test_day_is_the_utc_label_date_whatever_the_session_time_zone(
        self, conn: Conn, zone: str
    ) -> None:
        st = seed.station(conn)
        pl = seed.playlist(conn, st)
        ident = seed.identity(conn)
        before = seed.play(conn, pl, ident, at("23:59:59", PREV))
        inside = [
            seed.play(conn, pl, ident, at(hms))
            for hms in ("00:00:00", "00:30:00", "23:59:59", "23:59:59.999999")
        ]
        after = seed.play(conn, pl, ident, at("00:00:00", NEXT))
        set_zone(conn, zone)

        assert events(conn, st) == inside
        assert [r["logged_at"] for r in day_rows(conn, st)] == [
            wall("00:00:00"),
            wall("00:30:00"),
            wall("23:59:59"),
            wall("23:59:59.999999"),
        ]
        assert (events(conn, st, PREV), events(conn, st, NEXT)) == ([before], [after])

    def test_play_date_of_each_play_is_its_utc_label_date(self, conn: Conn) -> None:
        """Every play's own row, not only a filtered day, carries the UTC-label date."""
        st = seed.station(conn)
        pl = seed.playlist(conn, st)
        ident = seed.identity(conn)
        late = seed.play(conn, pl, ident, at("23:30", PREV))
        early = seed.play(conn, pl, ident, at("00:30"))
        set_zone(conn, "America/Chicago")

        rows = conn.execute(
            "SELECT play_event_id, play_date FROM station_day_plays WHERE play_event_id = ANY(%s)",
            ([late, early],),
        ).fetchall()

        assert sorted((str(r["play_event_id"]), r["play_date"]) for r in rows) == sorted(
            [(str(late), PREV), (str(early), DAY)]
        )

    def test_empty_day_and_unknown_station_have_no_rows(self, conn: Conn) -> None:
        st = seed.station(conn)
        seed.play(conn, seed.playlist(conn, st), seed.identity(conn), at("08:00", NEXT))

        assert day_rows(conn, st) == []
        assert day_rows(conn, UUID(int=0xDEAD)) == []


# --- membership ------------------------------------------------------------------------------


class TestMembership:
    def test_every_playlist_of_the_station_is_merged_and_other_stations_excluded(
        self, conn: Conn
    ) -> None:
        st, other = seed.station(conn), seed.station(conn)
        first, second = seed.playlist(conn, st), seed.playlist(conn, st)
        foreign = seed.playlist(conn, other)
        e1 = seed.play(conn, first, seed.identity(conn), at("06:00"))
        e3 = seed.play(conn, first, seed.identity(conn), at("06:20"))
        e2 = seed.play(conn, second, seed.identity(conn), at("06:10"))
        f1 = seed.play(conn, foreign, seed.identity(conn), at("06:05"))

        assert events(conn, st) == [e1, e2, e3]
        assert events(conn, other) == [f1]

    def test_every_play_appears_once_whatever_its_matches(self, conn: Conn) -> None:
        """No file resolution: unmatched plays are kept, several matches add no rows."""
        st = seed.station(conn)
        pl = seed.playlist(conn, st)
        many = seed.identity(conn)
        for confidence in (0.9, 0.9, 0.5):
            seed.match(conn, many, seed.library_file(conn), confidence=confidence)
        seed.match(conn, many, None)
        expected = [
            seed.play(conn, pl, many, at("06:00")),
            seed.play(conn, pl, seed.identity(conn), at("06:01")),
            seed.play(conn, pl, seed.identity(conn, status="pending"), at("06:02")),
            seed.play(conn, pl, seed.identity(conn, status="auto_rejected"), at("06:03")),
        ]

        assert events(conn, st) == expected


# --- the order (D4) --------------------------------------------------------------------------


class TestOrder:
    def test_order_is_played_at_then_identity_id(self, conn: Conn) -> None:
        st = seed.station(conn)
        pl = seed.playlist(conn, st)
        low = seed.identity(conn, identity_id=UUID(int=0xA))
        high = seed.identity(conn, identity_id=UUID(int=0xB))
        late_high = seed.play(conn, pl, high, at("06:00:00"))
        late_low = seed.play(conn, pl, low, at("06:00:00"))
        early_high = seed.play(conn, pl, high, at("05:59:59"))

        assert events(conn, st) == [early_high, late_low, late_high]

    def test_exact_tie_on_played_at_and_identity_goes_to_event_id(self, conn: Conn) -> None:
        """One identity at the same second on two playlists: both plays, a total order."""
        st = seed.station(conn)
        ident = seed.identity(conn)
        second = seed.play(
            conn, seed.playlist(conn, st), ident, at("09:00"), event_id=UUID(int=0x2)
        )
        first = seed.play(conn, seed.playlist(conn, st), ident, at("09:00"), event_id=UUID(int=0x1))

        assert events(conn, st) == [first, second]

    def test_positions_count_from_zero_per_station_day(self, conn: Conn) -> None:
        st, other = seed.station(conn), seed.station(conn)
        pl, foreign = seed.playlist(conn, st), seed.playlist(conn, other)
        for hms in ("06:00", "07:00", "08:00"):
            seed.play(conn, pl, seed.identity(conn), at(hms))
            seed.play(conn, foreign, seed.identity(conn), at(hms))
        seed.play(conn, pl, seed.identity(conn), at("05:00", NEXT))
        seed.play(conn, pl, seed.identity(conn), at("06:00", NEXT))

        assert [r["position"] for r in day_rows(conn, st)] == [0, 1, 2]
        assert [r["position"] for r in day_rows(conn, other)] == [0, 1, 2]
        assert [r["position"] for r in day_rows(conn, st, NEXT)] == [0, 1]

    @pytest.mark.parametrize("zone", ["America/Chicago", "UTC"])
    def test_positions_do_not_depend_on_the_session_time_zone(self, conn: Conn, zone: str) -> None:
        """Plays either side of UTC-label midnight stay in their own day's positions."""
        st = seed.station(conn)
        pl = seed.playlist(conn, st)
        ident = seed.identity(conn)
        seed.play(conn, pl, ident, at("23:00", PREV))
        first = seed.play(conn, pl, ident, at("00:00"))
        second = seed.play(conn, pl, ident, at("04:00"))
        set_zone(conn, zone)

        assert [(UUID(str(r["play_event_id"])), r["position"]) for r in day_rows(conn, st)] == [
            (first, 0),
            (second, 1),
        ]

"""Migration 0040: station_identity_plays stays equal to the play_events count it summarises.

Every way plays or a playlist's station change is applied, then the table is compared with
the count it replaces: COUNT(*) of play_events per (playlist's station, identity).
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

pytestmark = pytest.mark.integration

T0 = datetime(2001, 1, 1, tzinfo=UTC)

_EXPECTED = """
    SELECT pl.station_id, pe.identity_id, COUNT(*) AS play_count
    FROM play_events pe JOIN playlists pl ON pl.id = pe.playlist_id
    WHERE pl.station_id IS NOT NULL
    GROUP BY pl.station_id, pe.identity_id
"""


@pytest.fixture
def conn(migrated_db: str) -> Generator[psycopg.Connection[Any]]:
    with psycopg.connect(migrated_db, autocommit=True) as c:
        yield c


def _station(conn: psycopg.Connection[Any]) -> UUID:
    station_id = uuid4()
    conn.execute(
        "INSERT INTO stations (id, call_letters) VALUES (%s, %s)",
        (station_id, f"K{station_id.hex[:6]}"),
    )
    return station_id


def _playlist(conn: psycopg.Connection[Any], station_id: UUID | None) -> UUID:
    playlist_id = uuid4()
    conn.execute(
        "INSERT INTO playlists (id, name, content_hash, station_id) VALUES (%s, %s, %s, %s)",
        (playlist_id, "show.csv", uuid4().hex, station_id),
    )
    return playlist_id


def _identity(conn: psycopg.Connection[Any]) -> UUID:
    artist_id, identity_id = uuid4(), uuid4()
    conn.execute(
        """INSERT INTO broadcast_artists (id, original_name, normalized_name)
           VALUES (%s, %s, %s)""",
        (artist_id, "Artist", artist_id.hex),
    )
    conn.execute(
        """INSERT INTO track_identities
               (id, broadcast_artist_id, original_title, normalized_title, normalized_signature)
           VALUES (%s, %s, 'Song', 'song', %s)""",
        (identity_id, artist_id, identity_id.hex),
    )
    return identity_id


def _plays(conn: psycopg.Connection[Any], identity_id: UUID, playlist_id: UUID, n: int) -> None:
    """Insert n plays in one statement, minutes 1..n after T0."""
    conn.execute(
        """INSERT INTO play_events (identity_id, playlist_id, played_at)
           SELECT %s, %s, %s::timestamptz + make_interval(mins => g)
           FROM generate_series(1, %s) AS g""",
        (identity_id, playlist_id, T0, n),
    )


def _summary(conn: psycopg.Connection[Any]) -> set[tuple[Any, ...]]:
    rows = conn.execute(
        "SELECT station_id, identity_id, play_count FROM station_identity_plays"
    ).fetchall()
    return {tuple(r) for r in rows}


def _expected(conn: psycopg.Connection[Any]) -> set[tuple[Any, ...]]:
    return {tuple(r) for r in conn.execute(_EXPECTED).fetchall()}


@pytest.fixture
def seeded(conn: psycopg.Connection[Any]) -> dict[str, UUID]:
    """Two stations, three playlists (one without a station), two identities."""
    ids = {"a": _station(conn), "b": _station(conn)}
    ids["pa1"] = _playlist(conn, ids["a"])
    ids["pa2"] = _playlist(conn, ids["a"])
    ids["pb"] = _playlist(conn, ids["b"])
    ids["pn"] = _playlist(conn, None)
    ids["x"] = _identity(conn)
    ids["y"] = _identity(conn)
    _plays(conn, ids["x"], ids["pa1"], 5)
    _plays(conn, ids["x"], ids["pa2"], 3)
    _plays(conn, ids["y"], ids["pa1"], 2)
    _plays(conn, ids["x"], ids["pb"], 4)
    _plays(conn, ids["y"], ids["pn"], 6)
    return ids


def test_inserts_are_counted_per_station(conn: psycopg.Connection[Any], seeded: dict[str, UUID]):
    assert _summary(conn) == {
        (seeded["a"], seeded["x"], 8),
        (seeded["a"], seeded["y"], 2),
        (seeded["b"], seeded["x"], 4),
    }


def test_conflicting_insert_adds_nothing(conn: psycopg.Connection[Any], seeded: dict[str, UUID]):
    conn.execute(
        """INSERT INTO play_events (identity_id, playlist_id, played_at)
           VALUES (%s, %s, %s + interval '1 minute')
           ON CONFLICT (identity_id, playlist_id, played_at) DO NOTHING""",
        (seeded["x"], seeded["pa1"], T0),
    )

    assert _summary(conn) == _expected(conn)


def test_partial_delete(conn: psycopg.Connection[Any], seeded: dict[str, UUID]):
    conn.execute(
        "DELETE FROM play_events WHERE playlist_id = %s AND played_at <= %s + interval '2 min'",
        (seeded["pa1"], T0),
    )

    assert _summary(conn) == _expected(conn)
    assert (seeded["a"], seeded["y"], 2) not in _summary(conn)


def test_update_moving_plays_between_identities_and_playlists(
    conn: psycopg.Connection[Any], seeded: dict[str, UUID]
):
    conn.execute(
        "UPDATE play_events SET identity_id = %s WHERE identity_id = %s AND playlist_id = %s",
        (seeded["y"], seeded["x"], seeded["pa2"]),
    )
    conn.execute(
        "UPDATE play_events SET playlist_id = %s WHERE playlist_id = %s",
        (seeded["pb"], seeded["pn"]),
    )

    assert _summary(conn) == _expected(conn)


def test_update_leaving_keys_alone_changes_nothing(
    conn: psycopg.Connection[Any], seeded: dict[str, UUID]
):
    before = _summary(conn)
    conn.execute("UPDATE play_events SET played_at = played_at + interval '1 day'")

    assert _summary(conn) == before


def test_playlist_delete(conn: psycopg.Connection[Any], seeded: dict[str, UUID]):
    conn.execute("DELETE FROM playlists WHERE id = %s", (seeded["pa1"],))

    assert (
        _summary(conn)
        == _expected(conn)
        == {
            (seeded["a"], seeded["x"], 3),
            (seeded["b"], seeded["x"], 4),
        }
    )


def test_playlist_moves_between_stations_and_none(
    conn: psycopg.Connection[Any], seeded: dict[str, UUID]
):
    for playlist, station in (("pa1", "b"), ("pn", "a"), ("pb", None)):
        conn.execute(
            "UPDATE playlists SET station_id = %s WHERE id = %s",
            (seeded[station] if station else None, seeded[playlist]),
        )

        assert _summary(conn) == _expected(conn)


def test_station_delete(conn: psycopg.Connection[Any], seeded: dict[str, UUID]):
    conn.execute("DELETE FROM stations WHERE id = %s", (seeded["a"],))

    assert _summary(conn) == _expected(conn) == {(seeded["b"], seeded["x"], 4)}


def test_truncate_play_events(conn: psycopg.Connection[Any], seeded: dict[str, UUID]):
    conn.execute("TRUNCATE play_events")

    assert _summary(conn) == set()


def test_writes_in_one_transaction(conn: psycopg.Connection[Any], seeded: dict[str, UUID]):
    """Several statements in one transaction, rolled back, leave the counts as they were."""
    before = _summary(conn)
    with pytest.raises(psycopg.errors.RaiseException), conn.transaction():
        _plays(conn, seeded["y"], seeded["pb"], 7)
        conn.execute("DELETE FROM playlists WHERE id = %s", (seeded["pa2"],))
        assert _summary(conn) == _expected(conn)
        conn.execute("DO $$ BEGIN RAISE EXCEPTION 'roll back'; END $$")

    assert _summary(conn) == before

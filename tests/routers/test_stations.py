from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest

from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_days import PgBroadcastDayRepository
from backend.db.repositories.broadcast_play_events import PgBroadcastPlayEventRepository
from backend.db.repositories.broadcast_playlists import PgBroadcastPlaylistRepository
from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.domain.broadcast import (
    BroadcastArtist,
    BroadcastPlayEvent,
    BroadcastPlaylist,
    BroadcastStation,
    BroadcastTrackIdentity,
)
from backend.domain.enums import MatchStatus


def _insert_station(
    conn: psycopg.Connection, call_letters: str, **kwargs
) -> BroadcastStation:
    station = BroadcastStation(id=uuid4(), call_letters=call_letters, **kwargs)
    result = PgBroadcastStationRepository(conn).create(station)
    conn.commit()
    return result


def _insert_playlist(conn, station, name="show.csv"):
    playlist = BroadcastPlaylist(
        id=uuid4(), name=name, content_hash=uuid4().hex, station_id=station.id,
    )
    result = PgBroadcastPlaylistRepository(conn).create(playlist)
    conn.commit()
    return result


def _insert_event_full(
    conn, playlist, artist_name="Test Artist", title="Test Song", played_at=None
):
    """Insert artist + identity + event. Returns the event."""
    artist = BroadcastArtist(
        id=uuid4(), original_name=artist_name,
        normalized_name=artist_name.lower(), match_status=MatchStatus.PENDING,
    )
    PgBroadcastArtistRepository(conn).upsert(artist)

    identity = BroadcastTrackIdentity(
        id=uuid4(), broadcast_artist_id=artist.id, original_title=title,
        normalized_title=title.lower(),
        normalized_signature=f"{artist_name.lower()}:{title.lower()}",
        match_status=MatchStatus.PENDING,
    )
    PgBroadcastTrackIdentityRepository(conn).upsert(identity)

    event = BroadcastPlayEvent(
        id=uuid4(), identity_id=identity.id, playlist_id=playlist.id,
        played_at=played_at or datetime.now(tz=UTC),
    )
    PgBroadcastPlayEventRepository(conn).create(event)
    conn.commit()
    return event


def _bulk_insert_events(
    conn: psycopg.Connection,
    playlist: BroadcastPlaylist,
    rows: list[tuple[str, str, datetime]],
) -> None:
    """Bulk-insert (artist, identity, event) triples for a playlist.

    FK order is enforced by statement order (artists -> identities -> events);
    each stage uses executemany. Use for >= 5-row seed loops; smaller loops
    should continue calling _insert_event_full for readability.

    Callers MUST pass unique (artist_name, title) pairs: no ON CONFLICT clause
    is used, so duplicate seed rows will raise UniqueViolation at the artist or
    identity insert stage. Letting such failures surface is intentional — a
    silent skip on artists would leave downstream identities and play_events
    referencing an un-inserted row and produce a confusing FK violation.
    """
    artist_params: list[tuple] = []
    identity_params: list[tuple] = []
    event_params: list[tuple] = []
    for artist_name, title, played_at in rows:
        artist_id = uuid4()
        identity_id = uuid4()
        normalized_artist = artist_name.lower()
        normalized_title = title.lower()
        artist_params.append(
            (artist_id, artist_name, normalized_artist, MatchStatus.PENDING.value)
        )
        identity_params.append(
            (
                identity_id, artist_id, title, normalized_title,
                f"{normalized_artist}:{normalized_title}",
                MatchStatus.PENDING.value,
            )
        )
        event_params.append((uuid4(), identity_id, playlist.id, played_at))

    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO broadcast_artists "
            "(id, original_name, normalized_name, match_status) "
            "VALUES (%s, %s, %s, %s)",
            artist_params,
        )
        cur.executemany(
            "INSERT INTO track_identities "
            "(id, broadcast_artist_id, original_title, normalized_title, "
            " normalized_signature, match_status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            identity_params,
        )
        cur.executemany(
            "INSERT INTO play_events "
            "(id, identity_id, playlist_id, played_at) "
            "VALUES (%s, %s, %s, %s)",
            event_params,
        )
    conn.commit()


def _insert_artist_and_identity(
    conn: psycopg.Connection,
    artist_name: str,
    title: str,
    artist_status: MatchStatus = MatchStatus.PENDING,
    identity_status: MatchStatus = MatchStatus.PENDING,
) -> tuple[BroadcastArtist, BroadcastTrackIdentity]:
    """Create a broadcast artist + track identity with configurable match statuses.

    Returns the stored (artist, identity) pair so callers can attach play events
    to specific identities without re-querying.  Uses upsert so the returned
    objects always carry the IDs that are actually in the database.
    """
    artist = BroadcastArtist(
        id=uuid4(),
        original_name=artist_name,
        normalized_name=artist_name.lower(),
        match_status=artist_status,
    )
    stored_artist = PgBroadcastArtistRepository(conn).upsert(artist)

    identity = BroadcastTrackIdentity(
        id=uuid4(),
        broadcast_artist_id=stored_artist.id,
        original_title=title,
        normalized_title=title.lower(),
        normalized_signature=f"{artist_name.lower()}:{title.lower()}",
        match_status=identity_status,
    )
    stored_identity = PgBroadcastTrackIdentityRepository(conn).upsert(identity)
    conn.commit()
    return stored_artist, stored_identity


def _insert_play_events_for_identity(
    conn: psycopg.Connection,
    identity: BroadcastTrackIdentity,
    playlist: BroadcastPlaylist,
    n: int,
) -> None:
    """Insert *n* play events for an existing identity in a given playlist.

    Each event gets a unique played_at (second-level offsets from a fixed base)
    so the (identity_id, playlist_id, played_at) unique constraint is satisfied.
    Callers must pass n ≤ 59.
    """
    repo = PgBroadcastPlayEventRepository(conn)
    base = datetime(2001, 1, 1, tzinfo=UTC)
    for i in range(n):
        repo.create(
            BroadcastPlayEvent(
                id=uuid4(),
                identity_id=identity.id,
                playlist_id=playlist.id,
                played_at=base + timedelta(seconds=i),
            )
        )
    conn.commit()


def _insert_identity_with_events(
    conn: psycopg.Connection,
    playlist: BroadcastPlaylist,
    artist_name: str,
    title: str,
    n_events: int = 1,
    artist_status: MatchStatus = MatchStatus.PENDING,
    identity_status: MatchStatus = MatchStatus.PENDING,
) -> BroadcastTrackIdentity:
    """Convenience wrapper: create artist + identity + n play events in one call."""
    _, identity = _insert_artist_and_identity(
        conn, artist_name, title, artist_status, identity_status
    )
    _insert_play_events_for_identity(conn, identity, playlist, n_events)
    return identity


class TestListStations:
    def test_empty(self, client):
        resp = client.get("/api/v1/stations")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_all(self, client, db_conn):
        _insert_station(db_conn, "KAZR-FM", name="Laser 103.3", city="Waukee")
        _insert_station(db_conn, "KIOA-FM", name="KIOA", city="Des Moines")
        resp = client.get("/api/v1/stations")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["call_letters"] == "KAZR-FM"
        assert data[1]["call_letters"] == "KIOA-FM"

    def test_includes_playlist_count(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        PgBroadcastPlaylistRepository(db_conn).create(
            BroadcastPlaylist(
                id=uuid4(), name="test.csv", content_hash="abc123", station_id=station.id,
            )
        )
        db_conn.commit()
        resp = client.get("/api/v1/stations")
        data = resp.json()
        assert data[0]["playlist_count"] == 1


class TestCreateStation:
    def test_create(self, client):
        resp = client.post("/api/v1/stations", json={
            "call_letters": "KAZR-FM", "name": "Laser 103.3",
            "city": "Waukee", "format_name": "CHR",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["call_letters"] == "KAZR-FM"
        assert "id" in data

    def test_duplicate_call_letters_409(self, client, db_conn):
        _insert_station(db_conn, "KAZR-FM")
        resp = client.post("/api/v1/stations", json={"call_letters": "KAZR-FM"})
        assert resp.status_code == 409


class TestGetStation:
    def test_found(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM", name="Laser 103.3")
        resp = client.get(f"/api/v1/stations/{station.id}")
        assert resp.status_code == 200
        assert resp.json()["call_letters"] == "KAZR-FM"

    def test_not_found(self, client):
        resp = client.get(f"/api/v1/stations/{uuid4()}")
        assert resp.status_code == 404


class TestUpdateStation:
    def test_update(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM", name="Old Name")
        resp = client.put(f"/api/v1/stations/{station.id}", json={"name": "Laser 103.3"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Laser 103.3"
        assert resp.json()["call_letters"] == "KAZR-FM"

    def test_not_found(self, client):
        resp = client.put(f"/api/v1/stations/{uuid4()}", json={"name": "x"})
        assert resp.status_code == 404


class TestDeleteStation:
    def test_delete(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        resp = client.delete(f"/api/v1/stations/{station.id}")
        assert resp.status_code == 204
        resp = client.get(f"/api/v1/stations/{station.id}")
        assert resp.status_code == 404

    def test_not_found(self, client):
        resp = client.delete(f"/api/v1/stations/{uuid4()}")
        assert resp.status_code == 404

    def test_cascades_all_children(self, client, db_conn):
        """Station delete must cascade via BOTH FK paths to play_events:
        broadcast_day_id → broadcast_days → stations, AND
        playlist_id → playlists → stations.

        Two play_events rows are inserted to isolate each path:
          - event_both: linked to the deleted station's playlist AND
            broadcast_day — removable via either cascade.
          - event_broadcast_only: linked to the deleted station's
            broadcast_day but to a DIFFERENT station's playlist, so
            the playlist cascade cannot reach it. This row's deletion
            proves the broadcast_day_id path works independently.
        """
        station = _insert_station(db_conn, "KAZR-FM")
        broadcast_day = PgBroadcastDayRepository(db_conn).get_or_create(
            station.id, date(2001, 3, 15)
        )
        playlist = _insert_playlist(db_conn, station)

        # Second station + its own playlist. This playlist will NOT be deleted
        # when KAZR-FM is deleted, so any play_event referencing it can only
        # be removed via the broadcast_day_id cascade.
        other_station = _insert_station(db_conn, "KIOA-FM")
        other_playlist = _insert_playlist(db_conn, other_station, name="other.csv")

        artist = BroadcastArtist(
            id=uuid4(), original_name="The Clash",
            normalized_name="the clash", match_status=MatchStatus.PENDING,
        )
        PgBroadcastArtistRepository(db_conn).upsert(artist)
        identity = BroadcastTrackIdentity(
            id=uuid4(), broadcast_artist_id=artist.id,
            original_title="London Calling", normalized_title="london calling",
            normalized_signature="the clash:london calling",
            match_status=MatchStatus.PENDING,
        )
        PgBroadcastTrackIdentityRepository(db_conn).upsert(identity)

        event_both = uuid4()
        event_broadcast_only = uuid4()
        db_conn.execute(
            "INSERT INTO play_events "
            "(id, identity_id, playlist_id, broadcast_day_id, played_at) "
            "VALUES (%s, %s, %s, %s, NOW())",
            (event_both, identity.id, playlist.id, broadcast_day.id),
        )
        # broadcast_day_id points at the to-be-deleted station's calendar,
        # but playlist_id points at the other station's playlist.
        db_conn.execute(
            "INSERT INTO play_events "
            "(id, identity_id, playlist_id, broadcast_day_id, played_at) "
            "VALUES (%s, %s, %s, %s, NOW())",
            (event_broadcast_only, identity.id, other_playlist.id, broadcast_day.id),
        )
        db_conn.commit()

        resp = client.delete(f"/api/v1/stations/{station.id}")
        assert resp.status_code == 204

        def _count(table: str, where: str, param) -> int:
            row = db_conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", (param,)  # noqa: S608
            ).fetchone()
            return row["n"]

        assert _count("stations", "id = %s", station.id) == 0
        assert _count("broadcast_days", "station_id = %s", station.id) == 0
        assert _count("playlists", "station_id = %s", station.id) == 0
        # Both play_events rows gone: one via playlist cascade, one via
        # broadcast_day cascade (the only path that can reach it).
        assert _count("play_events", "id = %s", event_both) == 0
        assert _count("play_events", "id = %s", event_broadcast_only) == 0

        # The other station and its playlist must be untouched.
        assert _count("stations", "id = %s", other_station.id) == 1
        assert _count("playlists", "id = %s", other_playlist.id) == 1

        # Globally shared catalog rows must survive — they are deduped across
        # all stations. A future migration that accidentally cascades into
        # these tables must fail this test.
        assert _count("broadcast_artists", "id = %s", artist.id) == 1
        assert _count("track_identities", "id = %s", identity.id) == 1

    def test_delete_twice_returns_404(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        resp = client.delete(f"/api/v1/stations/{station.id}")
        assert resp.status_code == 204
        resp = client.delete(f"/api/v1/stations/{station.id}")
        assert resp.status_code == 404


class TestStationBroadcastDays:
    def test_returns_dates(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        repo = PgBroadcastDayRepository(db_conn)
        repo.get_or_create(station.id, date(2001, 3, 15))
        repo.get_or_create(station.id, date(2001, 6, 20))
        db_conn.commit()

        resp = client.get(f"/api/v1/stations/{station.id}/broadcast-days")
        assert resp.status_code == 200
        assert resp.json() == ["2001-03-15", "2001-06-20"]

    def test_empty(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        resp = client.get(f"/api/v1/stations/{station.id}/broadcast-days")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_not_found(self, client):
        resp = client.get(f"/api/v1/stations/{uuid4()}/broadcast-days")
        assert resp.status_code == 404


class TestStationEventsByDate:
    def test_returns_events(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station, name="morning.csv")
        _insert_event_full(
            db_conn, playlist, "The Clash", "London Calling",
            played_at=datetime(2001, 3, 15, 8, 0, 0, tzinfo=UTC),
        )
        _insert_event_full(
            db_conn, playlist, "Nirvana", "Smells Like Teen Spirit",
            played_at=datetime(2001, 3, 15, 9, 0, 0, tzinfo=UTC),
        )

        resp = client.get(f"/api/v1/stations/{station.id}/events?date=2001-03-15")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        assert data["items"][0]["artist_name"] == "The Clash"
        assert data["items"][0]["playlist_name"] == "morning.csv"

    def test_cross_playlist(self, client, db_conn):
        """Events from multiple playlists on the same date are returned."""
        station = _insert_station(db_conn, "KAZR-FM")
        p1 = _insert_playlist(db_conn, station, name="morning.csv")
        p2 = _insert_playlist(db_conn, station, name="evening.csv")
        _insert_event_full(
            db_conn, p1, "The Clash", "London Calling",
            played_at=datetime(2001, 3, 15, 8, 0, 0, tzinfo=UTC),
        )
        _insert_event_full(
            db_conn, p2, "Nirvana", "In Bloom",
            played_at=datetime(2001, 3, 15, 20, 0, 0, tzinfo=UTC),
        )

        resp = client.get(f"/api/v1/stations/{station.id}/events?date=2001-03-15")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        names = {item["playlist_name"] for item in data["items"]}
        assert names == {"morning.csv", "evening.csv"}

    def test_excludes_other_dates(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        _insert_event_full(
            db_conn, playlist, "The Clash", "London Calling",
            played_at=datetime(2001, 3, 15, 8, 0, 0, tzinfo=UTC),
        )
        _insert_event_full(
            db_conn, playlist, "Nirvana", "In Bloom",
            played_at=datetime(2001, 3, 16, 8, 0, 0, tzinfo=UTC),
        )

        resp = client.get(f"/api/v1/stations/{station.id}/events?date=2001-03-15")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_pagination(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        _bulk_insert_events(
            db_conn,
            playlist,
            [
                (f"Artist {i}", f"Song {i}",
                 datetime(2001, 3, 15, 8, i, 0, tzinfo=UTC))
                for i in range(5)
            ],
        )

        resp = client.get(f"/api/v1/stations/{station.id}/events?date=2001-03-15&limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2

    def test_not_found(self, client):
        resp = client.get(f"/api/v1/stations/{uuid4()}/events?date=2001-03-15")
        assert resp.status_code == 404


class TestStationExportM3u:
    def test_export_empty_date(self, client, db_conn):
        """A station with no events on that date returns a valid M3U header."""
        station = _insert_station(db_conn, "KAZR-FM")
        resp = client.post(
            f"/api/v1/stations/{station.id}/export-m3u",
            json={"date": "2001-03-15"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("audio/x-mpegurl")
        assert "Content-Disposition" in resp.headers
        assert "KAZR-FM-2001-03-15.m3u" in resp.headers["Content-Disposition"]
        assert resp.text.startswith("#EXTM3U")

    def test_not_found(self, client):
        resp = client.post(
            f"/api/v1/stations/{uuid4()}/export-m3u",
            json={"date": "2001-03-15"},
        )
        assert resp.status_code == 404


class TestMissingMatchesReport:
    """Integration tests for GET /api/v1/stations/{id}/reports/missing-matches."""

    def _url(self, station_id: UUID, qs: str = "") -> str:
        base = f"/api/v1/stations/{station_id}/reports/missing-matches"
        return f"{base}{qs}" if qs else base

    # ------------------------------------------------------------------
    # Guard rails
    # ------------------------------------------------------------------

    def test_not_found(self, client):
        """Unknown station → 404."""
        resp = client.get(self._url(uuid4()))
        assert resp.status_code == 404

    def test_empty_station(self, client, db_conn):
        """Station with no play events returns an empty page, not an error."""
        station = _insert_station(db_conn, "KAZR-FM")
        resp = client.get(self._url(station.id))
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    # ------------------------------------------------------------------
    # Inclusion / exclusion logic
    # ------------------------------------------------------------------

    def test_matched_statuses_excluded(self, client, db_conn):
        """AUTO_MATCHED and MANUAL_MATCHED identities must not appear.

        Both the identity *and* its artist must be matched — if the artist is
        still PENDING the OR condition would correctly surface the track.
        """
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        _insert_identity_with_events(
            db_conn, playlist, "Artist A", "Song A",
            n_events=3,
            artist_status=MatchStatus.AUTO_MATCHED,
            identity_status=MatchStatus.AUTO_MATCHED,
        )
        _insert_identity_with_events(
            db_conn, playlist, "Artist B", "Song B",
            n_events=3,
            artist_status=MatchStatus.MANUAL_MATCHED,
            identity_status=MatchStatus.MANUAL_MATCHED,
        )
        resp = client.get(self._url(station.id))
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_all_unresolved_statuses_included(self, client, db_conn):
        """PENDING, NEEDS_REVIEW, AUTO_REJECTED, MANUAL_REJECTED all appear."""
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        for i, status in enumerate([
            MatchStatus.PENDING,
            MatchStatus.NEEDS_REVIEW,
            MatchStatus.AUTO_REJECTED,
            MatchStatus.MANUAL_REJECTED,
        ]):
            _insert_identity_with_events(
                db_conn, playlist, f"Artist {i}", "Song",
                n_events=1, identity_status=status,
            )
        resp = client.get(self._url(station.id))
        assert resp.status_code == 200
        assert resp.json()["total"] == 4

    def test_artist_status_triggers_inclusion(self, client, db_conn):
        """A track with AUTO_MATCHED identity but a PENDING artist must appear.

        The OR condition on artist match_status ensures that an unresolved artist
        surfaces the track even when the identity pipeline has already matched it.
        """
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        _insert_identity_with_events(
            db_conn, playlist, "Pending Artist", "Matched Song",
            n_events=5,
            artist_status=MatchStatus.PENDING,
            identity_status=MatchStatus.AUTO_MATCHED,
        )
        resp = client.get(self._url(station.id))
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["artist_name"] == "Pending Artist"

    def test_response_shape(self, client, db_conn):
        """Every required field is present on each item."""
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        _insert_identity_with_events(db_conn, playlist, "Artist A", "Song A", n_events=1)
        resp = client.get(self._url(station.id))
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert set(item.keys()) == {
            "identity_id", "artist_name", "track_title",
            "track_status", "play_count", "impact_pct",
        }

    # ------------------------------------------------------------------
    # Play counts and impact percentage
    # ------------------------------------------------------------------

    def test_play_count_correct(self, client, db_conn):
        """play_count reflects the total number of play events for that identity."""
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        _insert_identity_with_events(
            db_conn, playlist, "The Clash", "London Calling", n_events=7,
        )
        resp = client.get(self._url(station.id))
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["play_count"] == 7
        assert item["impact_pct"] == pytest.approx(100.0, abs=0.01)

    def test_impact_pct_formula(self, client, db_conn):
        """impact_pct = (identity plays / total plays) * 100, verified numerically.

        4 plays + 1 play → 80 % and 20 % respectively.
        """
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        _insert_identity_with_events(db_conn, playlist, "Artist A", "Big Hit", n_events=4)
        _insert_identity_with_events(db_conn, playlist, "Artist B", "One Off", n_events=1)

        resp = client.get(self._url(station.id))
        assert resp.status_code == 200
        by_artist = {i["artist_name"]: i for i in resp.json()["items"]}
        assert by_artist["Artist A"]["impact_pct"] == pytest.approx(80.0, abs=0.01)
        assert by_artist["Artist B"]["impact_pct"] == pytest.approx(20.0, abs=0.01)

    def test_impact_pct_sums_to_100(self, client, db_conn):
        """impact_pct values across all identities sum to ~100 %."""
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        for i in range(5):
            _insert_identity_with_events(
                db_conn, playlist, f"Artist {i:02d}", "Song", n_events=i + 1,
            )
        resp = client.get(self._url(station.id) + "?limit=500")
        assert resp.status_code == 200
        total_impact = sum(item["impact_pct"] for item in resp.json()["items"])
        assert total_impact == pytest.approx(100.0, abs=0.1)

    def test_impact_pct_denominator_spans_pages(self, client, db_conn):
        """impact_pct is computed over the full unfiltered set, not just the page.

        5 identities each with 1 play → each should show 20 % on every page.
        The window function guarantees this even when limit < total.
        """
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        for i in range(5):
            _insert_identity_with_events(
                db_conn, playlist, f"Artist {i:02d}", "Song", n_events=1,
            )
        r1 = client.get(self._url(station.id, "?limit=2&offset=0"))
        r2 = client.get(self._url(station.id, "?limit=2&offset=2"))
        assert r1.status_code == r2.status_code == 200
        # Both pages must report the same full total.
        assert r1.json()["total"] == 5
        assert r2.json()["total"] == 5
        # Every item on every page reflects the full denominator.
        all_items = r1.json()["items"] + r2.json()["items"]
        for item in all_items:
            assert item["impact_pct"] == pytest.approx(20.0, abs=0.01)

    # ------------------------------------------------------------------
    # Ordering
    # ------------------------------------------------------------------

    def test_sorted_by_artist_then_title(self, client, db_conn):
        """Results are ordered alphabetically: artist name first, then track title."""
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        _insert_identity_with_events(db_conn, playlist, "Zappa", "Inca Roads", n_events=1)
        _insert_identity_with_events(db_conn, playlist, "Abba", "Waterloo", n_events=1)
        _insert_identity_with_events(db_conn, playlist, "Abba", "Dancing Queen", n_events=1)

        resp = client.get(self._url(station.id))
        assert resp.status_code == 200
        names = [
            (item["artist_name"], item["track_title"])
            for item in resp.json()["items"]
        ]
        assert names == [
            ("Abba", "Dancing Queen"),
            ("Abba", "Waterloo"),
            ("Zappa", "Inca Roads"),
        ]

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def test_pagination_total_and_page_size(self, client, db_conn):
        """total reflects the full result set; items is capped to limit."""
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        for i in range(5):
            _insert_identity_with_events(
                db_conn, playlist, f"Artist {i:02d}", "Song", n_events=1,
            )
        resp = client.get(self._url(station.id, "?limit=2&offset=0"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2

    def test_pagination_last_page(self, client, db_conn):
        """An offset past the last full page returns the remaining items only."""
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        for i in range(5):
            _insert_identity_with_events(
                db_conn, playlist, f"Artist {i:02d}", "Song", n_events=1,
            )
        resp = client.get(self._url(station.id, "?limit=2&offset=4"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 1

    def test_pagination_offset_beyond_end(self, client, db_conn):
        """offset >= total must still return the correct total, not 0.

        This guards against the COUNT(*) OVER() approach where an empty page
        (no rows returned) would incorrectly report total=0, making it
        indistinguishable from 'station has no missing matches'.
        """
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        for i in range(3):
            _insert_identity_with_events(
                db_conn, playlist, f"Artist {i:02d}", "Song", n_events=1,
            )
        resp = client.get(self._url(station.id, "?limit=10&offset=100"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3       # full count preserved
        assert data["items"] == []      # page is empty

    # ------------------------------------------------------------------
    # Station isolation and cross-playlist aggregation
    # ------------------------------------------------------------------

    def test_excludes_other_station(self, client, db_conn):
        """Play events from a different station must not appear in this report."""
        station_a = _insert_station(db_conn, "KAZR-FM")
        station_b = _insert_station(db_conn, "KIOA-FM")
        playlist_a = _insert_playlist(db_conn, station_a)
        playlist_b = _insert_playlist(db_conn, station_b)
        _insert_identity_with_events(
            db_conn, playlist_a, "Artist A", "Song A", n_events=10,
        )
        _insert_identity_with_events(
            db_conn, playlist_b, "Artist B", "Song B", n_events=5,
        )

        resp = client.get(self._url(station_a.id))
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["artist_name"] == "Artist A"
        assert data["items"][0]["play_count"] == 10

    def test_cross_playlist_play_counts_aggregated(self, client, db_conn):
        """The same identity played via two playlists: counts are summed correctly.

        3 plays in morning.csv + 2 plays in evening.csv → play_count = 5.
        The GROUP BY ti.id in the SQL aggregates across all playlist membership.
        """
        station = _insert_station(db_conn, "KAZR-FM")
        playlist_a = _insert_playlist(db_conn, station, name="morning.csv")
        playlist_b = _insert_playlist(db_conn, station, name="evening.csv")

        _, identity = _insert_artist_and_identity(
            db_conn, "The Clash", "London Calling",
        )
        _insert_play_events_for_identity(db_conn, identity, playlist_a, n=3)
        _insert_play_events_for_identity(db_conn, identity, playlist_b, n=2)

        resp = client.get(self._url(station.id))
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["play_count"] == 5
        assert data["items"][0]["impact_pct"] == pytest.approx(100.0, abs=0.01)

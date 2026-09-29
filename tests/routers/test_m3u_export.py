"""Acceptance tests: both M3U exports read which file plays from ``play_file_resolution``.

Spec D16-D22 (``docs/superpowers/specs/2026-09-27-tune-in-streaming-design.md``):

- which file plays for a logged play is defined once, in the curation view (D17). The work is
  the matched file's own ``library_files.work_id`` (the recording's work is only a legacy
  route), and the work decides: the override for the play's station format, else the song
  master (D22). The matched file never plays because it was matched, and nothing falls back;
- only a final file whose ``file_status`` is ``present`` is written (D21);
- a station's plays on a date, and their order, are broadcast's view ``station_day_plays``
  (D19): the date is the stored wall-clock date whatever the session TimeZone (D3), and plays
  in the same second are ordered by identity id, then event id (D4).

Every test goes through the HTTP endpoint against real PostgreSQL, so it checks the router
wiring as well as the result.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from tests.integration import stream_seed as seed
from tests.integration.stream_seed import DAY, Conn, at

# --- helpers ---------------------------------------------------------------------------------


def file_on_work(
    conn: Conn,
    work_id: str | None,
    *,
    status: str = "present",
    recording_id: str | None = None,
) -> UUID:
    """A library file whose own ``library_files.work_id`` is ``work_id``."""
    file_id = seed.library_file(conn, status=status, recording_id=recording_id)
    conn.execute("UPDATE library_files SET work_id = %s WHERE id = %s", (work_id, file_id))
    return file_id


def export_day(client: TestClient, station_id: UUID, day: date = DAY) -> str:
    resp = client.post(f"/api/v1/stations/{station_id}/export-m3u", json={"date": day.isoformat()})
    assert resp.status_code == 200
    return resp.text


def export_playlist(client: TestClient, playlist_id: UUID) -> str:
    resp = client.post(f"/api/v1/playlists/{playlist_id}/export-m3u")
    assert resp.status_code == 200
    return resp.text


def paths(m3u: str) -> list[str]:
    """The file lines of an M3U text, in order."""
    return [line for line in m3u.splitlines() if line and not line.startswith("#")]


# --- the work decides which file plays -------------------------------------------------------


class TestTheFilesOwnWorkReachesItsMaster:
    """The review found ``recordings.work_id`` reached 250 of 6,924 matched files' masters;
    the file's own work reaches all of them. The matched file below has no recording."""

    def test_station_export_plays_the_master_of_the_matched_files_own_work(
        self, client: TestClient, db_conn: Conn
    ) -> None:
        station = seed.station(db_conn)
        work = seed.work(db_conn)
        matched, master = file_on_work(db_conn, work), file_on_work(db_conn, work)
        seed.song_master(db_conn, work, master)
        seed.matched_play(db_conn, seed.playlist(db_conn, station), at("08:00"), matched)
        db_conn.commit()

        assert paths(export_day(client, station)) == [seed.file_path(db_conn, master)]

    def test_playlist_export_plays_the_master_of_the_matched_files_own_work(
        self, client: TestClient, db_conn: Conn
    ) -> None:
        playlist = seed.playlist(db_conn, seed.station(db_conn))
        work = seed.work(db_conn)
        matched, master = file_on_work(db_conn, work), file_on_work(db_conn, work)
        seed.song_master(db_conn, work, master)
        seed.matched_play(db_conn, playlist, at("08:00"), matched)
        db_conn.commit()

        assert paths(export_playlist(client, playlist)) == [seed.file_path(db_conn, master)]

    def test_the_files_own_work_beats_its_recordings_work(
        self, client: TestClient, db_conn: Conn
    ) -> None:
        """When the file's own work and its recording's work differ, the file's own work
        decides; the recording's work is only a route for a file that has no work."""
        station = seed.station(db_conn)
        own_work, recording_work = seed.work(db_conn), seed.work(db_conn)
        matched = file_on_work(
            db_conn, own_work, recording_id=seed.work_recording(db_conn, recording_work)
        )
        own_master = file_on_work(db_conn, own_work)
        seed.song_master(db_conn, own_work, own_master)
        seed.song_master(db_conn, recording_work, file_on_work(db_conn, recording_work))
        seed.matched_play(db_conn, seed.playlist(db_conn, station), at("08:00"), matched)
        db_conn.commit()

        assert paths(export_day(client, station)) == [seed.file_path(db_conn, own_master)]


class TestPlaylistExportScope:
    def test_playlist_export_writes_only_that_playlists_plays(
        self, client: TestClient, db_conn: Conn
    ) -> None:
        """The playlist export keeps its own list of plays: another log of the same station
        and day is not part of it."""
        station = seed.station(db_conn)
        ours, other = seed.playlist(db_conn, station), seed.playlist(db_conn, station)
        our_file, other_file = seed.mastered_file(db_conn), seed.mastered_file(db_conn)
        seed.matched_play(db_conn, ours, at("08:00"), our_file)
        seed.matched_play(db_conn, other, at("09:00"), other_file)
        db_conn.commit()

        assert paths(export_playlist(client, ours)) == [seed.file_path(db_conn, our_file)]


class TestTheStationFormatOverrideWins:
    def test_station_export_plays_the_override_for_the_stations_format(
        self, client: TestClient, db_conn: Conn
    ) -> None:
        station = seed.station(db_conn, format_name="AC")
        work = seed.work(db_conn)
        matched, master, override = (file_on_work(db_conn, work) for _ in range(3))
        seed.song_master(db_conn, work, master)
        seed.format_override(db_conn, work, "AC", override)
        seed.matched_play(db_conn, seed.playlist(db_conn, station), at("08:00"), matched)
        db_conn.commit()

        assert paths(export_day(client, station)) == [seed.file_path(db_conn, override)]

    def test_playlist_export_plays_the_override_for_its_stations_format(
        self, client: TestClient, db_conn: Conn
    ) -> None:
        """The playlist export used to take the format from the request, and the UI never
        sent one; the view uses the format of the station the playlist was logged on."""
        playlist = seed.playlist(db_conn, seed.station(db_conn, format_name="AC"))
        work = seed.work(db_conn)
        matched, master, override = (file_on_work(db_conn, work) for _ in range(3))
        seed.song_master(db_conn, work, master)
        seed.format_override(db_conn, work, "AC", override)
        seed.matched_play(db_conn, playlist, at("08:00"), matched)
        db_conn.commit()

        assert paths(export_playlist(client, playlist)) == [seed.file_path(db_conn, override)]


# --- plays with no file to write -------------------------------------------------------------


class TestNoFileIsWritten:
    def test_a_matched_file_whose_work_has_no_master_is_not_written(
        self, client: TestClient, db_conn: Conn
    ) -> None:
        """D22: the matched file is only the route to its work; with no override and no
        master there is no file, and nothing falls back to the matched file."""
        station = seed.station(db_conn)
        matched = file_on_work(db_conn, seed.work(db_conn))
        seed.matched_play(db_conn, seed.playlist(db_conn, station), at("08:00"), matched)
        db_conn.commit()

        assert paths(export_day(client, station)) == []

    def test_a_matched_file_with_no_work_is_not_written(
        self, client: TestClient, db_conn: Conn
    ) -> None:
        station = seed.station(db_conn)
        matched = seed.library_file(db_conn)
        seed.matched_play(db_conn, seed.playlist(db_conn, station), at("08:00"), matched)
        db_conn.commit()

        assert paths(export_day(client, station)) == []

    @pytest.mark.parametrize("status", ["missing", "deleted"])
    def test_a_master_that_is_not_present_is_not_written(
        self, client: TestClient, db_conn: Conn, status: str
    ) -> None:
        """D21: only a ``present`` final file is written. The matched file reaches the work
        both through its own work and through its recording, so this checks the status rule
        alone."""
        station = seed.station(db_conn)
        work = seed.work(db_conn)
        matched = file_on_work(db_conn, work, recording_id=seed.work_recording(db_conn, work))
        master = file_on_work(db_conn, work, status=status)
        seed.song_master(db_conn, work, master)
        seed.matched_play(db_conn, seed.playlist(db_conn, station), at("08:00"), matched)
        db_conn.commit()

        assert paths(export_day(client, station)) == []

    @pytest.mark.parametrize(
        "match_status", ["pending", "needs_review", "auto_rejected", "manual_rejected"]
    )
    def test_a_play_whose_identity_is_not_matched_is_not_written(
        self, client: TestClient, db_conn: Conn, match_status: str
    ) -> None:
        station = seed.station(db_conn)
        identity = seed.identity(db_conn, status=match_status)
        seed.match(db_conn, identity, seed.mastered_file(db_conn))
        seed.play(db_conn, seed.playlist(db_conn, station), identity, at("08:00"))
        db_conn.commit()

        assert paths(export_day(client, station)) == []


# --- a station's plays on a date -------------------------------------------------------------


class TestStationDay:
    def test_plays_are_exported_on_their_logged_date(
        self, client: TestClient, db_conn: Conn
    ) -> None:
        """D3/D19: the logged wall-clock date, whatever the session TimeZone. The test server
        runs America/Chicago, where ``played_at::date`` moves a 00:30 play to the day before."""
        station = seed.station(db_conn)
        playlist = seed.playlist(db_conn, station)
        late, early = seed.mastered_file(db_conn), seed.mastered_file(db_conn)
        next_day = DAY + timedelta(days=1)
        seed.matched_play(db_conn, playlist, at("23:30"), late)
        seed.matched_play(db_conn, playlist, at("00:30", next_day), early)
        db_conn.commit()

        assert paths(export_day(client, station, DAY)) == [seed.file_path(db_conn, late)]
        assert paths(export_day(client, station, next_day)) == [seed.file_path(db_conn, early)]

    def test_plays_in_the_same_second_are_ordered_by_identity(
        self, client: TestClient, db_conn: Conn
    ) -> None:
        """D4/D19: ``station_day_plays.position`` breaks a ``played_at`` tie by identity id.
        The later identity's play is logged first, so insertion order is the wrong answer."""
        station = seed.station(db_conn)
        playlist = seed.playlist(db_conn, station)
        first, second = seed.mastered_file(db_conn), seed.mastered_file(db_conn)
        first_identity = seed.identity(db_conn, identity_id=UUID(int=1))
        second_identity = seed.identity(db_conn, identity_id=UUID(int=2))
        seed.match(db_conn, first_identity, first)
        seed.match(db_conn, second_identity, second)
        seed.play(db_conn, playlist, second_identity, at("08:00"))
        seed.play(db_conn, playlist, first_identity, at("08:00"))
        db_conn.commit()

        assert paths(export_day(client, station)) == [
            seed.file_path(db_conn, first),
            seed.file_path(db_conn, second),
        ]

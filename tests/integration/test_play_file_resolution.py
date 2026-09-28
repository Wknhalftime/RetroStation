"""Acceptance tests: the curation view ``play_file_resolution`` (spec D16, D17).

One row per play event, every play included, with the columns ``play_event_id``,
``station_id``, ``file_id``, ``file_status`` and ``source``. ``file_id``, ``file_status`` and
``source`` are NULL when the play does not resolve. Resolution follows D16:

- only ``auto_matched`` and ``manual_matched`` identities resolve;
- the best match is the highest confidence, then the earliest ``created_at``, then the lowest
  match id, and a match without a file is never chosen;
- the work is the direct file's own ``library_files.work_id``, else its recording's work;
  ``matches.work_id`` plays no role and there is no per-level fallback;
- override (for the play's station ``format_name``) > song master > direct file.

The view does not filter by status: it reports the final file and its ``file_status``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.integration import stream_seed as seed
from tests.integration.stream_seed import Conn, at

T0 = datetime(2026, 1, 1, tzinfo=UTC)

type Resolution = tuple[UUID | None, str | None, str | None]
"""(file_id, file_status, source) of one play."""

UNRESOLVED: Resolution = (None, None, None)


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def view_rows(conn: Conn, event_id: UUID) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT play_event_id, station_id, file_id, file_status, source
           FROM play_file_resolution WHERE play_event_id = %s""",
        (event_id,),
    ).fetchall()


def resolution(conn: Conn, event_id: UUID) -> Resolution:
    """The one view row of ``event_id``, as (file_id, file_status, source)."""
    [row] = view_rows(conn, event_id)
    file_id = row["file_id"]
    return (
        None if file_id is None else UUID(str(file_id)),
        row["file_status"],
        row["source"],
    )


def file_on_work(
    conn: Conn,
    work_id: str | None,
    *,
    recording_id: str | None = None,
    status: str = "present",
) -> UUID:
    """A library file whose own ``library_files.work_id`` is ``work_id``."""
    file_id = seed.library_file(conn, status=status, recording_id=recording_id)
    conn.execute("UPDATE library_files SET work_id = %s WHERE id = %s", (work_id, file_id))
    return file_id


def played(conn: Conn, direct: UUID, *, format_name: str | None = "AC") -> UUID:
    """A play, on a fresh station of ``format_name``, whose one match is ``direct``."""
    st = seed.station(conn, format_name=format_name)
    return seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), direct)


# --- rows ------------------------------------------------------------------------------------


class TestRows:
    def test_resolved_play_maps_every_column(self, conn: Conn) -> None:
        st = seed.station(conn)
        direct = seed.library_file(conn)
        event = seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), direct)

        [row] = view_rows(conn, event)

        assert (
            UUID(str(row["play_event_id"])),
            UUID(str(row["station_id"])),
            UUID(str(row["file_id"])),
            row["file_status"],
            row["source"],
        ) == (event, st, direct, "present", "direct")

    def test_unresolved_play_has_a_row_with_null_file_status_and_source(self, conn: Conn) -> None:
        st = seed.station(conn)
        event = seed.play(conn, seed.playlist(conn, st), seed.identity(conn), at("08:00"))

        [row] = view_rows(conn, event)

        assert UUID(str(row["station_id"])) == st
        assert (row["file_id"], row["file_status"], row["source"]) == UNRESOLVED

    def test_exactly_one_row_per_play_whatever_its_matches(self, conn: Conn) -> None:
        st = seed.station(conn)
        pl = seed.playlist(conn, st)
        work = seed.work(conn)
        seed.song_master(conn, work, file_on_work(conn, work))
        many = seed.identity(conn)
        for confidence in (0.9, 0.9, 0.5):
            seed.match(conn, many, file_on_work(conn, work), confidence=confidence)
        seed.match(conn, many, None, confidence=0.99)
        events = [
            seed.play(conn, pl, many, at("08:00")),
            seed.play(conn, pl, many, at("09:00")),
            seed.play(conn, seed.playlist(conn, st), many, at("08:00")),
            seed.play(conn, pl, seed.identity(conn), at("10:00")),
            seed.play(conn, pl, seed.identity(conn, status="pending"), at("11:00")),
        ]

        counts = conn.execute(
            """SELECT play_event_id, count(*) AS n FROM play_file_resolution
               WHERE play_event_id = ANY(%s) GROUP BY play_event_id""",
            (events,),
        ).fetchall()

        assert sorted((str(r["play_event_id"]), r["n"]) for r in counts) == sorted(
            (str(e), 1) for e in events
        )

    def test_station_id_is_the_play_station(self, conn: Conn) -> None:
        first, second = seed.station(conn), seed.station(conn)
        ident = seed.identity(conn)
        on_first = seed.play(conn, seed.playlist(conn, first), ident, at("08:00"))
        on_second = seed.play(conn, seed.playlist(conn, second), ident, at("08:00"))

        [a], [b] = view_rows(conn, on_first), view_rows(conn, on_second)

        assert (UUID(str(a["station_id"])), UUID(str(b["station_id"]))) == (first, second)


# --- identity status (D16) -------------------------------------------------------------------


class TestIdentityStatus:
    @pytest.mark.parametrize(
        "status", ["pending", "needs_review", "auto_rejected", "manual_rejected"]
    )
    def test_unmatched_identity_does_not_resolve(self, conn: Conn, status: str) -> None:
        st = seed.station(conn)
        ident = seed.identity(conn, status=status)
        seed.match(conn, ident, seed.library_file(conn))
        event = seed.play(conn, seed.playlist(conn, st), ident, at("08:00"))

        assert resolution(conn, event) == UNRESOLVED

    @pytest.mark.parametrize("status", ["auto_matched", "manual_matched"])
    def test_matched_identity_resolves(self, conn: Conn, status: str) -> None:
        st = seed.station(conn)
        direct = seed.library_file(conn)
        ident = seed.identity(conn, status=status)
        seed.match(conn, ident, direct)
        event = seed.play(conn, seed.playlist(conn, st), ident, at("08:00"))

        assert resolution(conn, event) == (direct, "present", "direct")

    def test_matched_identity_without_a_match_row_does_not_resolve(self, conn: Conn) -> None:
        event = seed.play(
            conn, seed.playlist(conn, seed.station(conn)), seed.identity(conn), at("08:00")
        )

        assert resolution(conn, event) == UNRESOLVED


# --- best match (D16) ------------------------------------------------------------------------


def _play_of(conn: Conn, ident: UUID) -> UUID:
    return seed.play(conn, seed.playlist(conn, seed.station(conn)), ident, at("08:00"))


class TestBestMatch:
    def test_highest_confidence_wins_over_an_earlier_match(self, conn: Conn) -> None:
        ident = seed.identity(conn)
        seed.match(conn, ident, seed.library_file(conn), confidence=0.7, created_at=T0)
        best = seed.library_file(conn)
        seed.match(conn, ident, best, confidence=0.95, created_at=T0 + timedelta(hours=1))

        assert resolution(conn, _play_of(conn, ident))[0] == best

    def test_confidence_tie_goes_to_the_earliest_match(self, conn: Conn) -> None:
        ident = seed.identity(conn)
        later = T0 + timedelta(hours=1)
        seed.match(conn, ident, seed.library_file(conn), confidence=0.9, created_at=later)
        earliest = seed.library_file(conn)
        seed.match(conn, ident, earliest, confidence=0.9, created_at=T0)

        assert resolution(conn, _play_of(conn, ident))[0] == earliest

    def test_full_tie_goes_to_the_lowest_match_id(self, conn: Conn) -> None:
        ident = seed.identity(conn)
        seed.match(conn, ident, seed.library_file(conn), created_at=T0, match_id=UUID(int=0x2))
        lowest = seed.library_file(conn)
        seed.match(conn, ident, lowest, created_at=T0, match_id=UUID(int=0x1))

        assert resolution(conn, _play_of(conn, ident))[0] == lowest

    def test_match_without_a_file_is_never_the_best_match(self, conn: Conn) -> None:
        ident = seed.identity(conn)
        seed.match(conn, ident, None, confidence=0.99)
        real = seed.library_file(conn)
        seed.match(conn, ident, real, confidence=0.5)

        assert resolution(conn, _play_of(conn, ident)) == (real, "present", "direct")

    def test_only_matches_without_a_file_do_not_resolve(self, conn: Conn) -> None:
        ident = seed.identity(conn)
        seed.match(conn, ident, None)

        assert resolution(conn, _play_of(conn, ident)) == UNRESOLVED

    def test_only_the_best_match_decides_the_work(self, conn: Conn) -> None:
        ident = seed.identity(conn)
        other_work = seed.work(conn)
        seed.song_master(conn, other_work, file_on_work(conn, other_work))
        seed.match(conn, ident, file_on_work(conn, other_work), confidence=0.5)
        best = file_on_work(conn, None)
        seed.match(conn, ident, best, confidence=0.9)

        assert resolution(conn, _play_of(conn, ident)) == (best, "present", "direct")


# --- the file's own work decides (D16) -------------------------------------------------------


class TestFileWork:
    def test_master_of_the_file_work_without_a_recording(self, conn: Conn) -> None:
        """Test 1: ``library_files.work_id`` alone reaches the song master."""
        work = seed.work(conn)
        direct, master = file_on_work(conn, work), file_on_work(conn, work)
        seed.song_master(conn, work, master)

        assert resolution(conn, played(conn, direct)) == (master, "present", "master")

    def test_override_of_the_file_work_beats_its_master(self, conn: Conn) -> None:
        """Test 2: the station-format override beats the master."""
        work = seed.work(conn)
        direct, master, override = (file_on_work(conn, work) for _ in range(3))
        seed.song_master(conn, work, master)
        seed.format_override(conn, work, "AC", override)

        assert resolution(conn, played(conn, direct)) == (override, "present", "override")

    def test_file_work_beats_the_recording_work(self, conn: Conn) -> None:
        """Test 3: the two works disagree; the master of the file's OWN work wins."""
        own_work, recording_work = seed.work(conn), seed.work(conn)
        own_master = file_on_work(conn, own_work)
        seed.song_master(conn, own_work, own_master)
        seed.song_master(conn, recording_work, seed.work_file(conn, recording_work))
        direct = file_on_work(
            conn, own_work, recording_id=seed.work_recording(conn, recording_work)
        )

        assert resolution(conn, played(conn, direct)) == (own_master, "present", "master")

    def test_recording_work_override_is_not_reached(self, conn: Conn) -> None:
        """Test 3b: an override on the recording's work is not reached; the own master is."""
        own_work, recording_work = seed.work(conn), seed.work(conn)
        own_master = file_on_work(conn, own_work)
        seed.song_master(conn, own_work, own_master)
        seed.format_override(conn, recording_work, "AC", seed.work_file(conn, recording_work))
        direct = file_on_work(
            conn, own_work, recording_id=seed.work_recording(conn, recording_work)
        )

        assert resolution(conn, played(conn, direct)) == (own_master, "present", "master")

    def test_recording_work_is_the_fallback_when_the_file_has_no_work(self, conn: Conn) -> None:
        """Test 4: ``library_files.work_id`` NULL, so the recording's work (legacy guard)."""
        work = seed.work(conn)
        direct = file_on_work(conn, None, recording_id=seed.work_recording(conn, work))
        master = seed.work_file(conn, work)
        seed.song_master(conn, work, master)

        assert resolution(conn, played(conn, direct)) == (master, "present", "master")

    def test_each_station_format_resolves_the_same_identity_its_own_way(self, conn: Conn) -> None:
        """Test 5: one identity on an AC and a CHR station; the override is for AC only."""
        work = seed.work(conn)
        direct, master, override = (file_on_work(conn, work) for _ in range(3))
        seed.song_master(conn, work, master)
        seed.format_override(conn, work, "AC", override)
        ident = seed.identity(conn)
        seed.match(conn, ident, direct)
        on_ac = seed.play(conn, seed.playlist(conn, seed.station(conn, "AC")), ident, at("08:00"))
        on_chr = seed.play(conn, seed.playlist(conn, seed.station(conn, "CHR")), ident, at("08:00"))

        assert (resolution(conn, on_ac), resolution(conn, on_chr)) == (
            (override, "present", "override"),
            (master, "present", "master"),
        )

    def test_stale_match_work_id_is_ignored(self, conn: Conn) -> None:
        """Test A: ``matches.work_id`` pointing at another work plays no role."""
        own_work, stale_work = seed.work(conn), seed.work(conn)
        own_master = file_on_work(conn, own_work)
        seed.song_master(conn, own_work, own_master)
        seed.song_master(conn, stale_work, file_on_work(conn, stale_work))
        direct = file_on_work(conn, own_work)
        ident = seed.identity(conn)
        match_id = seed.match(conn, ident, direct)
        conn.execute("UPDATE matches SET work_id = %s WHERE id = %s", (stale_work, match_id))

        assert resolution(conn, _play_of(conn, ident)) == (own_master, "present", "master")

    def test_no_per_level_fallback_to_the_recording_work(self, conn: Conn) -> None:
        """Test B: the own work has no master or override, so the DIRECT file.

        The recording's work has both a master and an AC override; neither is reached.
        """
        own_work, recording_work = seed.work(conn), seed.work(conn)
        seed.song_master(conn, recording_work, seed.work_file(conn, recording_work))
        seed.format_override(conn, recording_work, "AC", seed.work_file(conn, recording_work))
        direct = file_on_work(
            conn, own_work, recording_id=seed.work_recording(conn, recording_work)
        )

        assert resolution(conn, played(conn, direct)) == (direct, "present", "direct")

    def test_file_work_decides_when_the_recording_has_no_work(self, conn: Conn) -> None:
        """Test C: the common real-data shape, a file work and a recording without one."""
        work = seed.work(conn)
        master = file_on_work(conn, work)
        seed.song_master(conn, work, master)
        direct = file_on_work(conn, work, recording_id=seed.recording(conn, None))

        assert resolution(conn, played(conn, direct)) == (master, "present", "master")

    def test_file_without_any_work_is_played_as_is(self, conn: Conn) -> None:
        direct = file_on_work(conn, None, recording_id=seed.recording(conn, None))

        assert resolution(conn, played(conn, direct)) == (direct, "present", "direct")


# --- station format (D16) --------------------------------------------------------------------


class TestStationFormat:
    @pytest.mark.parametrize("with_master", [True, False], ids=["master", "no-master"])
    def test_station_without_a_format_gets_no_override(self, conn: Conn, with_master: bool) -> None:
        work = seed.work(conn)
        direct, master, override = (file_on_work(conn, work) for _ in range(3))
        if with_master:
            seed.song_master(conn, work, master)
        seed.format_override(conn, work, "AC", override)

        expected = (master, "present", "master") if with_master else (direct, "present", "direct")
        assert resolution(conn, played(conn, direct, format_name=None)) == expected

    def test_override_for_another_format_is_ignored(self, conn: Conn) -> None:
        work = seed.work(conn)
        direct, master, override = (file_on_work(conn, work) for _ in range(3))
        seed.song_master(conn, work, master)
        seed.format_override(conn, work, "CHR", override)

        assert resolution(conn, played(conn, direct)) == (master, "present", "master")

    def test_override_applies_without_a_master(self, conn: Conn) -> None:
        work = seed.work(conn)
        direct, override = file_on_work(conn, work), file_on_work(conn, work)
        seed.format_override(conn, work, "AC", override)

        assert resolution(conn, played(conn, direct)) == (override, "present", "override")


# --- no status filter: the final file is reported as it is (D16, D17) -----------------------


class TestFinalFileStatus:
    @pytest.mark.parametrize("status", ["missing", "deleted"])
    @pytest.mark.parametrize("source", ["direct", "master", "override"])
    def test_unavailable_final_file_is_reported_with_its_status_and_no_fallback(
        self, conn: Conn, source: str, status: str
    ) -> None:
        """Test 6 (view): the view reports the unavailable file; it never falls back."""
        work = seed.work(conn)
        direct = file_on_work(conn, work, status=status if source == "direct" else "present")
        final = direct
        if source in ("master", "override"):
            final = file_on_work(conn, work, status=status if source == "master" else "present")
            seed.song_master(conn, work, final)
        if source == "override":
            final = file_on_work(conn, work, status=status)
            seed.format_override(conn, work, "AC", final)

        assert resolution(conn, played(conn, direct)) == (final, status, source)

    def test_missing_direct_file_still_resolves_to_a_present_master(self, conn: Conn) -> None:
        work = seed.work(conn)
        direct = file_on_work(conn, work, status="missing")
        master = file_on_work(conn, work)
        seed.song_master(conn, work, master)

        assert resolution(conn, played(conn, direct)) == (master, "present", "master")

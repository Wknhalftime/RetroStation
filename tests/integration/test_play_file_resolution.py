"""Acceptance tests: the curation view ``play_file_resolution`` (spec D16, D17, D22).

One row per play event, every play included, with the columns ``play_event_id``,
``station_id``, ``matched_file_id``, ``work_id``, ``file_id``, ``file_status`` and ``source``.

- only ``auto_matched`` and ``manual_matched`` identities are matched;
- the best match is the highest confidence, then the earliest ``created_at``, then the lowest
  match id, and a match without a file is never chosen. Its file is ``matched_file_id``;
- the matched file is only the route to the work (D22): ``work_id`` is the matched file's own
  ``library_files.work_id``, else its recording's work. ``matches.work_id`` plays no role;
- the work decides what plays: the override for the play's station ``format_name``, else the
  song master (``source`` is ``override`` or ``master``). There is no ``direct`` source and no
  per-level fallback: no work, or neither, leaves ``file_id``, ``file_status`` and ``source``
  NULL while ``matched_file_id`` (and ``work_id``, if any) say why.

The view does not filter by status: it reports the final file and its ``file_status``.

DRAFT for D22: replaces ``test_play_file_resolution.py`` once the user approves.
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

type Route = tuple[UUID | None, str | None]
"""(matched_file_id, work_id) of one play."""

UNRESOLVED: Resolution = (None, None, None)
UNMATCHED: Route = (None, None)


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def view_rows(conn: Conn, event_id: UUID) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT play_event_id, station_id, matched_file_id, work_id,
                  file_id, file_status, source
           FROM play_file_resolution WHERE play_event_id = %s""",
        (event_id,),
    ).fetchall()


def _uuid(value: object) -> UUID | None:
    return None if value is None else UUID(str(value))


def resolution(conn: Conn, event_id: UUID) -> Resolution:
    """The one view row of ``event_id``, as (file_id, file_status, source)."""
    [row] = view_rows(conn, event_id)
    return _uuid(row["file_id"]), row["file_status"], row["source"]


def route(conn: Conn, event_id: UUID) -> Route:
    """The one view row of ``event_id``, as (matched_file_id, work_id)."""
    [row] = view_rows(conn, event_id)
    return _uuid(row["matched_file_id"]), row["work_id"]


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


def work_of(conn: Conn, file_id: UUID) -> str:
    row = conn.execute("SELECT work_id FROM library_files WHERE id = %s", (file_id,)).fetchone()
    assert row is not None
    return str(row["work_id"])


def played(conn: Conn, matched: UUID, *, format_name: str | None = "AC") -> UUID:
    """A play, on a fresh station of ``format_name``, whose one match is ``matched``."""
    st = seed.station(conn, format_name=format_name)
    return seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), matched)


# --- rows ------------------------------------------------------------------------------------


class TestRows:
    def test_resolved_play_maps_every_column(self, conn: Conn) -> None:
        st = seed.station(conn)
        work = seed.work(conn)
        matched, master = file_on_work(conn, work), file_on_work(conn, work)
        seed.song_master(conn, work, master)
        event = seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), matched)

        [row] = view_rows(conn, event)

        assert (
            UUID(str(row["play_event_id"])),
            UUID(str(row["station_id"])),
            UUID(str(row["matched_file_id"])),
            row["work_id"],
            UUID(str(row["file_id"])),
            row["file_status"],
            row["source"],
        ) == (event, st, matched, work, master, "present", "master")

    def test_unmatched_play_has_a_row_of_nulls(self, conn: Conn) -> None:
        st = seed.station(conn)
        event = seed.play(conn, seed.playlist(conn, st), seed.identity(conn), at("08:00"))

        [row] = view_rows(conn, event)

        assert UUID(str(row["station_id"])) == st
        assert (row["matched_file_id"], row["work_id"]) == UNMATCHED
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
        seed.match(conn, ident, seed.mastered_file(conn))
        event = seed.play(conn, seed.playlist(conn, st), ident, at("08:00"))

        assert (route(conn, event), resolution(conn, event)) == (UNMATCHED, UNRESOLVED)

    @pytest.mark.parametrize("status", ["auto_matched", "manual_matched"])
    def test_matched_identity_resolves(self, conn: Conn, status: str) -> None:
        st = seed.station(conn)
        matched = seed.mastered_file(conn)
        ident = seed.identity(conn, status=status)
        seed.match(conn, ident, matched)
        event = seed.play(conn, seed.playlist(conn, st), ident, at("08:00"))

        assert resolution(conn, event) == (matched, "present", "master")

    def test_matched_identity_without_a_match_row_does_not_resolve(self, conn: Conn) -> None:
        event = seed.play(
            conn, seed.playlist(conn, seed.station(conn)), seed.identity(conn), at("08:00")
        )

        assert (route(conn, event), resolution(conn, event)) == (UNMATCHED, UNRESOLVED)


# --- best match (D16) ------------------------------------------------------------------------


def _play_of(conn: Conn, ident: UUID) -> UUID:
    return seed.play(conn, seed.playlist(conn, seed.station(conn)), ident, at("08:00"))


class TestBestMatch:
    """Each candidate is its own work's master, so the file that plays names the best match."""

    def test_highest_confidence_wins_over_an_earlier_match(self, conn: Conn) -> None:
        ident = seed.identity(conn)
        seed.match(conn, ident, seed.mastered_file(conn), confidence=0.7, created_at=T0)
        best = seed.mastered_file(conn)
        seed.match(conn, ident, best, confidence=0.95, created_at=T0 + timedelta(hours=1))

        event = _play_of(conn, ident)
        assert (route(conn, event)[0], resolution(conn, event)[0]) == (best, best)

    def test_confidence_tie_goes_to_the_earliest_match(self, conn: Conn) -> None:
        ident = seed.identity(conn)
        later = T0 + timedelta(hours=1)
        seed.match(conn, ident, seed.mastered_file(conn), confidence=0.9, created_at=later)
        earliest = seed.mastered_file(conn)
        seed.match(conn, ident, earliest, confidence=0.9, created_at=T0)

        event = _play_of(conn, ident)
        assert (route(conn, event)[0], resolution(conn, event)[0]) == (earliest, earliest)

    def test_full_tie_goes_to_the_lowest_match_id(self, conn: Conn) -> None:
        ident = seed.identity(conn)
        seed.match(conn, ident, seed.mastered_file(conn), created_at=T0, match_id=UUID(int=0x2))
        lowest = seed.mastered_file(conn)
        seed.match(conn, ident, lowest, created_at=T0, match_id=UUID(int=0x1))

        event = _play_of(conn, ident)
        assert (route(conn, event)[0], resolution(conn, event)[0]) == (lowest, lowest)

    def test_match_without_a_file_is_never_the_best_match(self, conn: Conn) -> None:
        ident = seed.identity(conn)
        seed.match(conn, ident, None, confidence=0.99)
        real = seed.mastered_file(conn)
        seed.match(conn, ident, real, confidence=0.5)

        assert resolution(conn, _play_of(conn, ident)) == (real, "present", "master")

    def test_only_matches_without_a_file_do_not_resolve(self, conn: Conn) -> None:
        ident = seed.identity(conn)
        seed.match(conn, ident, None)

        event = _play_of(conn, ident)
        assert (route(conn, event), resolution(conn, event)) == (UNMATCHED, UNRESOLVED)

    def test_only_the_best_match_decides_the_work(self, conn: Conn) -> None:
        """The weaker match's work has a master; the best match has no work: no master."""
        ident = seed.identity(conn)
        other_work = seed.work(conn)
        seed.song_master(conn, other_work, file_on_work(conn, other_work))
        seed.match(conn, ident, file_on_work(conn, other_work), confidence=0.5)
        best = file_on_work(conn, None)
        seed.match(conn, ident, best, confidence=0.9)

        event = _play_of(conn, ident)
        assert (route(conn, event), resolution(conn, event)) == ((best, None), UNRESOLVED)


# --- the matched file's own work decides (D16, D22) ------------------------------------------


class TestFileWork:
    def test_master_of_the_file_work_without_a_recording(self, conn: Conn) -> None:
        """Test 1: ``library_files.work_id`` alone reaches the song master."""
        work = seed.work(conn)
        matched, master = file_on_work(conn, work), file_on_work(conn, work)
        seed.song_master(conn, work, master)

        assert resolution(conn, played(conn, matched)) == (master, "present", "master")

    def test_override_of_the_file_work_beats_its_master(self, conn: Conn) -> None:
        """Test 2: the station-format override beats the master."""
        work = seed.work(conn)
        matched, master, override = (file_on_work(conn, work) for _ in range(3))
        seed.song_master(conn, work, master)
        seed.format_override(conn, work, "AC", override)

        assert resolution(conn, played(conn, matched)) == (override, "present", "override")

    def test_file_work_beats_the_recording_work(self, conn: Conn) -> None:
        """Test 3: the two works disagree; the master of the file's OWN work wins."""
        own_work, recording_work = seed.work(conn), seed.work(conn)
        own_master = file_on_work(conn, own_work)
        seed.song_master(conn, own_work, own_master)
        seed.song_master(conn, recording_work, seed.work_file(conn, recording_work))
        matched = file_on_work(
            conn, own_work, recording_id=seed.work_recording(conn, recording_work)
        )

        event = played(conn, matched)
        assert route(conn, event) == (matched, own_work)
        assert resolution(conn, event) == (own_master, "present", "master")

    def test_recording_work_override_is_not_reached(self, conn: Conn) -> None:
        """Test 3b: an override on the recording's work is not reached; the own master is."""
        own_work, recording_work = seed.work(conn), seed.work(conn)
        own_master = file_on_work(conn, own_work)
        seed.song_master(conn, own_work, own_master)
        seed.format_override(conn, recording_work, "AC", seed.work_file(conn, recording_work))
        matched = file_on_work(
            conn, own_work, recording_id=seed.work_recording(conn, recording_work)
        )

        assert resolution(conn, played(conn, matched)) == (own_master, "present", "master")

    def test_recording_work_is_the_fallback_when_the_file_has_no_work(self, conn: Conn) -> None:
        """Test 4: ``library_files.work_id`` NULL, so the recording's work (legacy guard)."""
        work = seed.work(conn)
        matched = file_on_work(conn, None, recording_id=seed.work_recording(conn, work))
        master = seed.work_file(conn, work)
        seed.song_master(conn, work, master)

        event = played(conn, matched)
        assert route(conn, event) == (matched, work)
        assert resolution(conn, event) == (master, "present", "master")

    def test_each_station_format_resolves_the_same_identity_its_own_way(self, conn: Conn) -> None:
        """Test 5: one identity on an AC and a CHR station; the override is for AC only."""
        work = seed.work(conn)
        matched, master, override = (file_on_work(conn, work) for _ in range(3))
        seed.song_master(conn, work, master)
        seed.format_override(conn, work, "AC", override)
        ident = seed.identity(conn)
        seed.match(conn, ident, matched)
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
        matched = file_on_work(conn, own_work)
        ident = seed.identity(conn)
        match_id = seed.match(conn, ident, matched)
        conn.execute("UPDATE matches SET work_id = %s WHERE id = %s", (stale_work, match_id))

        event = _play_of(conn, ident)
        assert route(conn, event) == (matched, own_work)
        assert resolution(conn, event) == (own_master, "present", "master")

    def test_no_per_level_fallback_to_the_recording_work(self, conn: Conn) -> None:
        """Test B: the own work has no master or override, so the play has no master (D22).

        The recording's work has both a master and an AC override; neither is reached, and
        the matched file does not play either.
        """
        own_work, recording_work = seed.work(conn), seed.work(conn)
        seed.song_master(conn, recording_work, seed.work_file(conn, recording_work))
        seed.format_override(conn, recording_work, "AC", seed.work_file(conn, recording_work))
        matched = file_on_work(
            conn, own_work, recording_id=seed.work_recording(conn, recording_work)
        )

        event = played(conn, matched)
        assert (route(conn, event), resolution(conn, event)) == ((matched, own_work), UNRESOLVED)

    def test_file_work_decides_when_the_recording_has_no_work(self, conn: Conn) -> None:
        """Test C: the common real-data shape, a file work and a recording without one."""
        work = seed.work(conn)
        master = file_on_work(conn, work)
        seed.song_master(conn, work, master)
        matched = file_on_work(conn, work, recording_id=seed.recording(conn, None))

        assert resolution(conn, played(conn, matched)) == (master, "present", "master")


# --- no master (D22) -------------------------------------------------------------------------


class TestNoMaster:
    def test_work_with_neither_override_nor_master_does_not_resolve(self, conn: Conn) -> None:
        work = seed.work(conn)
        matched = file_on_work(conn, work)

        event = played(conn, matched)
        assert (route(conn, event), resolution(conn, event)) == ((matched, work), UNRESOLVED)

    @pytest.mark.parametrize("recording", [False, True], ids=["no-recording", "workless-recording"])
    def test_matched_file_without_any_work_does_not_resolve(
        self, conn: Conn, recording: bool
    ) -> None:
        recording_id = seed.recording(conn, None) if recording else None
        matched = file_on_work(conn, None, recording_id=recording_id)

        event = played(conn, matched)
        assert (route(conn, event), resolution(conn, event)) == ((matched, None), UNRESOLVED)

    def test_no_master_through_the_recording_work_names_that_work(self, conn: Conn) -> None:
        """A workless file reaches its recording's work W; W has no master: route names W."""
        work = seed.work(conn)
        matched = file_on_work(conn, None, recording_id=seed.work_recording(conn, work))

        event = played(conn, matched)
        assert (route(conn, event), resolution(conn, event)) == ((matched, work), UNRESOLVED)

    def test_the_matched_file_never_plays_for_being_matched(self, conn: Conn) -> None:
        """Even a present, cued matched file: without a master or override it is not played."""
        work = seed.work(conn)
        audio = seed.audio_hash()
        matched = seed.library_file(conn, audio_hash=audio)
        conn.execute("UPDATE library_files SET work_id = %s WHERE id = %s", (work, matched))
        seed.cue_row(conn, audio)

        assert resolution(conn, played(conn, matched)) == UNRESOLVED

    def test_route_is_exposed_for_every_matched_play(self, conn: Conn) -> None:
        """Resolved, no-master and no-work plays all carry their matched file and work."""
        resolved = seed.mastered_file(conn)
        workless = file_on_work(conn, None)
        masterless_work = seed.work(conn)
        masterless = file_on_work(conn, masterless_work)

        assert [route(conn, played(conn, f)) for f in (resolved, masterless, workless)] == [
            (resolved, work_of(conn, resolved)),
            (masterless, masterless_work),
            (workless, None),
        ]

    def test_source_is_only_override_master_or_null(self, conn: Conn) -> None:
        """No ``direct`` value: one day of every kind of play, and the sources it produces."""
        work = seed.work(conn)
        seed.song_master(conn, work, file_on_work(conn, work))
        other = seed.work(conn)
        seed.format_override(conn, other, "AC", file_on_work(conn, other))
        events = [
            played(conn, file_on_work(conn, work)),  # master
            played(conn, file_on_work(conn, other)),  # override without master
            played(conn, file_on_work(conn, seed.work(conn))),  # no master
            played(conn, file_on_work(conn, None)),  # no work
            seed.play(
                conn, seed.playlist(conn, seed.station(conn)), seed.identity(conn), at("08:00")
            ),  # unmatched
        ]

        rows = conn.execute(
            "SELECT source FROM play_file_resolution WHERE play_event_id = ANY(%s)", (events,)
        ).fetchall()

        assert sorted(str(r["source"]) for r in rows) == sorted(
            ["master", "override", "None", "None", "None"]
        )


# --- station format (D16) --------------------------------------------------------------------


class TestStationFormat:
    def test_station_without_a_format_gets_no_override(self, conn: Conn) -> None:
        work = seed.work(conn)
        matched, master, override = (file_on_work(conn, work) for _ in range(3))
        seed.song_master(conn, work, master)
        seed.format_override(conn, work, "AC", override)

        assert resolution(conn, played(conn, matched, format_name=None)) == (
            master,
            "present",
            "master",
        )

    def test_station_without_a_format_and_a_work_without_a_master_does_not_resolve(
        self, conn: Conn
    ) -> None:
        """The work's only file choice is an override for another format: no master (D22)."""
        work = seed.work(conn)
        matched, override = file_on_work(conn, work), file_on_work(conn, work)
        seed.format_override(conn, work, "AC", override)

        event = played(conn, matched, format_name=None)
        assert (route(conn, event), resolution(conn, event)) == ((matched, work), UNRESOLVED)

    def test_override_for_another_format_is_ignored(self, conn: Conn) -> None:
        work = seed.work(conn)
        matched, master, override = (file_on_work(conn, work) for _ in range(3))
        seed.song_master(conn, work, master)
        seed.format_override(conn, work, "CHR", override)

        assert resolution(conn, played(conn, matched)) == (master, "present", "master")

    def test_override_applies_without_a_master(self, conn: Conn) -> None:
        work = seed.work(conn)
        matched, override = file_on_work(conn, work), file_on_work(conn, work)
        seed.format_override(conn, work, "AC", override)

        assert resolution(conn, played(conn, matched)) == (override, "present", "override")


# --- no status filter: the final file is reported as it is (D16, D17) -----------------------


class TestFinalFileStatus:
    @pytest.mark.parametrize("status", ["missing", "deleted"])
    @pytest.mark.parametrize("source", ["master", "override"])
    def test_unavailable_final_file_is_reported_with_its_status_and_no_fallback(
        self, conn: Conn, source: str, status: str
    ) -> None:
        """Test 6 (view): the view reports the unavailable file; it never falls back."""
        work = seed.work(conn)
        matched = file_on_work(conn, work)
        final = file_on_work(conn, work, status=status if source == "master" else "present")
        seed.song_master(conn, work, final)
        if source == "override":
            final = file_on_work(conn, work, status=status)
            seed.format_override(conn, work, "AC", final)

        assert resolution(conn, played(conn, matched)) == (final, status, source)

    def test_missing_matched_file_still_resolves_to_a_present_master(self, conn: Conn) -> None:
        work = seed.work(conn)
        matched = file_on_work(conn, work, status="missing")
        master = file_on_work(conn, work)
        seed.song_master(conn, work, master)

        assert resolution(conn, played(conn, matched)) == (master, "present", "master")

    @pytest.mark.parametrize("status", ["missing", "deleted"])
    def test_unavailable_matched_file_that_is_its_own_master_is_reported(
        self, conn: Conn, status: str
    ) -> None:
        """The matched file is its work's master and is unavailable: source is ``master``."""
        matched = seed.mastered_file(conn, status=status)

        assert resolution(conn, played(conn, matched)) == (matched, status, "master")

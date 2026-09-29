"""Acceptance tests: the playable schedule reader over PostgreSQL (spec: Data, D3-D4, D16-D22).

``PgPlayableScheduleRepository.get_day(station_id, day)`` returns one station-day of
``ScheduleItem``s: every logged play, in ``(played_at, identity_id, event_id)`` order, each resolved
to the file its work plays (the station format's override, else the song master; D22), with the
cue points of that file's audio or none (D20).

A test that wants "this matched file plays" seeds ``seed.mastered_file``: a file that is its own
work's master. The matched file never plays merely for being matched (D22).

DRAFT for D22: replaces ``test_playable_schedule.py`` once the user approves.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row
from structlog.testing import capture_logs

from backend.db.repositories.playable_schedule import PgPlayableScheduleRepository
from backend.db.repositories.stream_cues import PgStreamCueRepository
from backend.domain.library import AudioHash
from backend.domain.streaming import (
    CUE_ANALYSER_VERSION,
    CueAnalysis,
    CuePoints,
    PlayableFile,
    ScheduleItem,
)
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import (
    CUE_POINTS,
    DAY,
    FILE_MTIME_NS,
    FILE_SIZE,
    VERSION,
    Conn,
    at,
    wall,
)

PREV = DAY - timedelta(days=1)
NEXT = DAY + timedelta(days=1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def get_day(conn: Conn, station_id: UUID) -> list[ScheduleItem]:
    return PgPlayableScheduleRepository(conn).get_day(station_id, DAY)


def only_file(conn: Conn, station_id: UUID) -> PlayableFile | None:
    items = get_day(conn, station_id)
    assert len(items) == 1
    return items[0].file


def resolved_id(conn: Conn, station_id: UUID) -> UUID | None:
    file = only_file(conn, station_id)
    return None if file is None else file.file_id


def warnings(logs: Sequence[MutableMapping[str, Any]], event: str) -> list[Any]:
    return [e for e in logs if e["event"] == event and e["log_level"] == "warning"]


def all_warnings(logs: Sequence[MutableMapping[str, Any]]) -> list[str]:
    return [e["event"] for e in logs if e["log_level"] == "warning"]


# --- every logged play, in order -------------------------------------------------------------


class TestPlays:
    def test_resolved_play_maps_every_field(self, conn: Conn) -> None:
        st = seed.station(conn)
        pl = seed.playlist(conn, st)
        file_id = seed.mastered_file(conn, duration_ms=187_000)
        ident = seed.identity(conn, title="Hold On", artist="Wilson Phillips")
        seed.match(conn, ident, file_id)
        event = seed.play(conn, pl, ident, at("07:15:42"))

        assert get_day(conn, st) == [
            ScheduleItem(
                event_id=event,
                logged_at=wall("07:15:42"),
                title="Hold On",
                artist="Wilson Phillips",
                file=PlayableFile(
                    file_id=file_id,
                    path=seed.file_path(conn, file_id),
                    duration_ms=187_000,
                    cues=None,
                ),
            )
        ]

    def test_file_without_duration_keeps_none(self, conn: Conn) -> None:
        st = seed.station(conn)
        seed.matched_play(
            conn, seed.playlist(conn, st), at("07:00"), seed.mastered_file(conn, duration_ms=None)
        )
        file = only_file(conn, st)
        assert file is not None
        assert file.duration_ms is None

    @pytest.mark.parametrize(
        "status", ["pending", "needs_review", "auto_rejected", "manual_rejected"]
    )
    def test_play_of_an_unmatched_identity_is_kept_with_no_file(
        self, conn: Conn, status: str
    ) -> None:
        st = seed.station(conn)
        ident = seed.identity(conn, title="Unknown", artist="Nobody", status=status)
        seed.match(conn, ident, seed.library_file(conn))
        event = seed.play(conn, seed.playlist(conn, st), ident, at("08:00"))

        assert get_day(conn, st) == [
            ScheduleItem(
                event_id=event, logged_at=wall("08:00"), title="Unknown", artist="Nobody", file=None
            )
        ]

    @pytest.mark.parametrize("status", ["auto_matched", "manual_matched"])
    def test_play_of_a_matched_identity_resolves(self, conn: Conn, status: str) -> None:
        st = seed.station(conn)
        file_id = seed.mastered_file(conn)
        ident = seed.identity(conn, status=status)
        seed.match(conn, ident, file_id)
        seed.play(conn, seed.playlist(conn, st), ident, at("08:00"))

        assert resolved_id(conn, st) == file_id

    def test_matched_identity_without_a_match_row_has_no_file(self, conn: Conn) -> None:
        st = seed.station(conn)
        seed.play(conn, seed.playlist(conn, st), seed.identity(conn), at("08:00"))

        assert resolved_id(conn, st) is None

    def test_every_playlist_of_the_station_is_merged_and_other_stations_excluded(
        self, conn: Conn
    ) -> None:
        st, other = seed.station(conn), seed.station(conn)
        first, second, foreign = (
            seed.playlist(conn, st),
            seed.playlist(conn, st),
            seed.playlist(conn, other),
        )
        e1 = seed.play(conn, first, seed.identity(conn), at("06:00"))
        e3 = seed.play(conn, first, seed.identity(conn), at("06:20"))
        e2 = seed.play(conn, second, seed.identity(conn), at("06:10"))
        seed.play(conn, foreign, seed.identity(conn), at("06:05"))

        assert [item.event_id for item in get_day(conn, st)] == [e1, e2, e3]

    def test_order_is_played_at_then_identity_id(self, conn: Conn) -> None:
        st = seed.station(conn)
        pl = seed.playlist(conn, st)
        low = seed.identity(conn, identity_id=UUID(int=0xA))
        high = seed.identity(conn, identity_id=UUID(int=0xB))
        late_high = seed.play(conn, pl, high, at("06:00:00"))
        late_low = seed.play(conn, pl, low, at("06:00:00"))
        early_high = seed.play(conn, pl, high, at("05:59:59"))

        assert [item.event_id for item in get_day(conn, st)] == [early_high, late_low, late_high]

    def test_same_play_logged_twice_is_kept_twice_in_event_id_order(self, conn: Conn) -> None:
        """One identity at the same second on two playlists: both plays, a total order.

        The domain indexes a day by position, so the order must never depend on the plan.
        """
        st = seed.station(conn)
        ident = seed.identity(conn)
        second = seed.play(
            conn, seed.playlist(conn, st), ident, at("09:00"), event_id=UUID(int=0x2)
        )
        first = seed.play(conn, seed.playlist(conn, st), ident, at("09:00"), event_id=UUID(int=0x1))

        assert [item.event_id for item in get_day(conn, st)] == [first, second]

    def test_day_without_plays_and_unknown_station_are_empty(self, conn: Conn) -> None:
        st = seed.station(conn)
        seed.play(conn, seed.playlist(conn, st), seed.identity(conn), at("08:00", NEXT))

        assert get_day(conn, st) == []
        assert get_day(conn, UUID(int=0xDEAD)) == []


# --- the day window (D3) ---------------------------------------------------------------------


class TestDayWindow:
    @pytest.mark.parametrize("zone", ["Asia/Tokyo", "America/Chicago", "UTC"])
    def test_day_is_the_utc_label_date_whatever_the_session_time_zone(
        self, conn: Conn, zone: str
    ) -> None:
        st = seed.station(conn)
        pl = seed.playlist(conn, st)
        ident = seed.identity(conn)
        for played_at in (
            at("23:30", PREV),
            at("00:00"),
            at("00:30"),
            at("23:30"),
            at("23:59:59.999999"),
            at("00:00", NEXT),
            at("00:30", NEXT),
        ):
            seed.play(conn, pl, ident, played_at)
        conn.execute("SELECT set_config('TimeZone', %s, false)", (zone,))
        shown = conn.execute("SHOW TimeZone").fetchone()
        assert shown is not None
        assert shown["TimeZone"] == zone

        assert [item.logged_at for item in get_day(conn, st)] == [
            wall("00:00"),
            wall("00:30"),
            wall("23:30"),
            wall("23:59:59.999999"),
        ]


# --- best match (D16) ------------------------------------------------------------------------


class TestBestMatch:
    """Each candidate is its own work's master, so the file that plays names the best match."""

    def test_highest_confidence_wins_over_an_earlier_match(self, conn: Conn) -> None:
        st = seed.station(conn)
        ident = seed.identity(conn)
        seed.match(conn, ident, seed.mastered_file(conn), confidence=0.7, created_at=T0)
        best = seed.mastered_file(conn)
        seed.match(conn, ident, best, confidence=0.95, created_at=T0 + timedelta(hours=1))
        seed.play(conn, seed.playlist(conn, st), ident, at("08:00"))

        assert resolved_id(conn, st) == best

    def test_confidence_tie_goes_to_the_earliest_match(self, conn: Conn) -> None:
        st = seed.station(conn)
        ident = seed.identity(conn)
        seed.match(
            conn,
            ident,
            seed.mastered_file(conn),
            confidence=0.9,
            created_at=T0 + timedelta(hours=1),
        )
        earliest = seed.mastered_file(conn)
        seed.match(conn, ident, earliest, confidence=0.9, created_at=T0)
        seed.play(conn, seed.playlist(conn, st), ident, at("08:00"))

        assert resolved_id(conn, st) == earliest

    def test_full_tie_goes_to_the_lowest_match_id(self, conn: Conn) -> None:
        st = seed.station(conn)
        ident = seed.identity(conn)
        seed.match(conn, ident, seed.mastered_file(conn), created_at=T0, match_id=UUID(int=0x2))
        lowest = seed.mastered_file(conn)
        seed.match(conn, ident, lowest, created_at=T0, match_id=UUID(int=0x1))
        seed.play(conn, seed.playlist(conn, st), ident, at("08:00"))

        assert resolved_id(conn, st) == lowest

    def test_match_row_without_a_file_is_never_the_best_match(self, conn: Conn) -> None:
        st = seed.station(conn)
        ident = seed.identity(conn)
        seed.match(conn, ident, None, confidence=0.99)
        real = seed.mastered_file(conn)
        seed.match(conn, ident, real, confidence=0.5)
        seed.play(conn, seed.playlist(conn, st), ident, at("08:00"))

        assert resolved_id(conn, st) == real

    def test_only_the_best_match_decides_the_work(self, conn: Conn) -> None:
        """The weaker match's work has a master; the best match has no work: no master."""
        st = seed.station(conn)
        ident = seed.identity(conn)
        other_work = seed.work(conn)
        seed.song_master(conn, other_work, seed.work_file(conn, other_work))
        seed.match(conn, ident, seed.work_file(conn, other_work), confidence=0.5)
        best = seed.library_file(conn, recording_id=None)
        seed.match(conn, ident, best, confidence=0.9)
        seed.play(conn, seed.playlist(conn, st), ident, at("08:00"))

        with capture_logs() as logs:
            file = only_file(conn, st)

        assert file is None
        [warning] = warnings(logs, "schedule_no_master")
        assert (str(warning["matched_file_id"]), warning["work_id"]) == (str(best), None)


# --- master and override (D16, D22) ----------------------------------------------------------


class TestResolution:
    def test_song_master_beats_the_matched_file(self, conn: Conn) -> None:
        st = seed.station(conn)
        work = seed.work(conn)
        matched, master = seed.work_file(conn, work), seed.work_file(conn, work)
        seed.song_master(conn, work, master)
        seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), matched)

        file = only_file(conn, st)
        assert file is not None
        assert (file.file_id, file.path) == (master, seed.file_path(conn, master))

    def test_override_for_the_station_format_beats_the_master(self, conn: Conn) -> None:
        st = seed.station(conn, format_name="AC")
        work = seed.work(conn)
        matched, master, override = (seed.work_file(conn, work) for _ in range(3))
        seed.song_master(conn, work, master)
        seed.format_override(conn, work, "AC", override)
        seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), matched)

        assert resolved_id(conn, st) == override

    def test_override_for_another_format_is_ignored(self, conn: Conn) -> None:
        st = seed.station(conn, format_name="AC")
        work = seed.work(conn)
        matched, master, override = (seed.work_file(conn, work) for _ in range(3))
        seed.song_master(conn, work, master)
        seed.format_override(conn, work, "CHR", override)
        seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), matched)

        assert resolved_id(conn, st) == master

    def test_override_applies_without_a_master(self, conn: Conn) -> None:
        st = seed.station(conn, format_name="AC")
        work = seed.work(conn)
        matched, override = seed.work_file(conn, work), seed.work_file(conn, work)
        seed.format_override(conn, work, "AC", override)
        seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), matched)

        assert resolved_id(conn, st) == override

    def test_station_without_a_format_uses_no_override(self, conn: Conn) -> None:
        st = seed.station(conn, format_name=None)
        work = seed.work(conn)
        matched, master, override = (seed.work_file(conn, work) for _ in range(3))
        seed.song_master(conn, work, master)
        seed.format_override(conn, work, "AC", override)
        seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), matched)

        assert resolved_id(conn, st) == master

    def test_missing_matched_file_still_resolves_to_a_present_master(self, conn: Conn) -> None:
        st = seed.station(conn)
        work = seed.work(conn)
        matched = seed.work_file(conn, work, status="missing")
        master = seed.work_file(conn, work)
        seed.song_master(conn, work, master)
        seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), matched)

        assert resolved_id(conn, st) == master


# --- missing and deleted files (D16) ---------------------------------------------------------


def _chain_with_unavailable(conn: Conn, st: UUID, source: str, status: str) -> tuple[UUID, UUID]:
    """A play whose final file (chosen by rule ``source``) has ``status``; the others are present.

    Returns (event id, unavailable file id).
    """
    work = seed.work(conn)
    matched = seed.work_file(conn, work)
    final = seed.work_file(conn, work, status=status if source == "master" else "present")
    seed.song_master(conn, work, final)
    if source == "override":
        final = seed.work_file(conn, work, status=status)
        seed.format_override(conn, work, "AC", final)
    event = seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), matched)
    return event, final


class TestUnavailableFiles:
    @pytest.mark.parametrize("status", ["missing", "deleted"])
    @pytest.mark.parametrize("source", ["master", "override"])
    def test_unavailable_final_file_gives_no_file_no_fallback_and_a_warning(
        self, conn: Conn, source: str, status: str
    ) -> None:
        st = seed.station(conn, format_name="AC")
        event, final = _chain_with_unavailable(conn, st, source, status)

        with capture_logs() as logs:
            items = get_day(conn, st)

        assert [(item.event_id, item.file) for item in items] == [(event, None)]
        [warning] = warnings(logs, "schedule_file_unavailable")
        assert str(warning["event_id"]) == str(event)
        assert str(warning["file_id"]) == str(final)
        assert str(warning["file_status"]) == status
        assert str(warning["source"]) == source

    def test_one_warning_per_affected_play(self, conn: Conn) -> None:
        st = seed.station(conn)
        pl = seed.playlist(conn, st)
        work = seed.work(conn)
        matched = seed.work_file(conn, work)
        seed.song_master(conn, work, seed.work_file(conn, work, status="missing"))
        ident = seed.identity(conn)
        seed.match(conn, ident, matched)
        morning = seed.play(conn, pl, ident, at("08:00"))
        evening = seed.play(conn, pl, ident, at("20:00"))
        seed.matched_play(conn, pl, at("12:00"), seed.mastered_file(conn))

        with capture_logs() as logs:
            get_day(conn, st)

        logged = [str(w["event_id"]) for w in warnings(logs, "schedule_file_unavailable")]
        assert sorted(logged) == sorted([str(morning), str(evening)])
        assert all_warnings(logs) == ["schedule_file_unavailable"] * 2

    def test_clean_day_logs_no_schedule_warning(self, conn: Conn) -> None:
        st = seed.station(conn)
        pl = seed.playlist(conn, st)
        file_id, _ = seed.cued_master(conn)
        seed.matched_play(conn, pl, at("08:00"), file_id)
        seed.play(conn, pl, seed.identity(conn, status="pending"), at("09:00"))

        with capture_logs() as logs:
            get_day(conn, st)

        assert all_warnings(logs) == []


# --- cue points: the audio's, if it has any (D20) --------------------------------------------


def _file_with_cues(conn: Conn, **row: object) -> UUID:
    """A station with one resolved play whose file's audio has a cue row (``CUE_ROW`` + ``row``)."""
    st = seed.station(conn)
    audio = seed.audio_hash()
    file_id = seed.mastered_file(conn, audio_hash=audio)
    seed.cue_row(conn, audio, **row)
    seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), file_id)
    return st


def _cues_of_one_play(conn: Conn, file_id: UUID) -> CuePoints | None:
    """The cues a one-play day resolved to ``file_id`` (a mastered file) reads for it."""
    st = seed.station(conn)
    seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), file_id)
    file = only_file(conn, st)
    assert file is not None
    return file.cues


def _set_file_column(conn: Conn, file_id: UUID, column: str, value: object) -> None:
    assert column in {"file_size", "file_mtime_ns", "audio_hash"}
    conn.execute(f"UPDATE library_files SET {column} = %s WHERE id = %s", (value, file_id))


class TestCues:
    def test_row_for_the_file_audio_becomes_cue_points(self, conn: Conn) -> None:
        file = only_file(conn, _file_with_cues(conn))
        assert file is not None
        assert file.cues == CUE_POINTS

    def test_audio_without_a_row_gives_no_cues(self, conn: Conn) -> None:
        seed.cued_master(conn)  # another audio has cues
        file_id = seed.mastered_file(conn, audio_hash=seed.audio_hash())

        assert _cues_of_one_play(conn, file_id) is None

    def test_file_without_an_audio_hash_gives_no_cues(self, conn: Conn) -> None:
        seed.cued_master(conn)  # some audio has cues
        file_id = seed.mastered_file(conn, audio_hash=None)

        assert _cues_of_one_play(conn, file_id) is None

    @pytest.mark.parametrize(
        "version",
        [1, VERSION, CUE_ANALYSER_VERSION + 1, 99],
        ids=["one", "seeded", "newer-than-current", "far-future"],
    )
    def test_analyser_version_is_not_checked(self, conn: Conn, version: int) -> None:
        file = only_file(conn, _file_with_cues(conn, analyser_version=version))
        assert file is not None
        assert file.cues == CUE_POINTS

    @pytest.mark.parametrize(
        "stat",
        [
            {"file_size": FILE_SIZE + 1},
            {"file_mtime_ns": FILE_MTIME_NS + 1},
            {"file_size": None, "file_mtime_ns": None},
        ],
        ids=["size", "mtime", "unknown"],
    )
    def test_file_stat_is_not_checked(self, conn: Conn, stat: dict[str, object]) -> None:
        """A moved or retagged file keeps its audio, so it keeps its cues (D20)."""
        file_id, _ = seed.cued_master(conn)
        for column, value in stat.items():
            _set_file_column(conn, file_id, column, value)

        assert _cues_of_one_play(conn, file_id) == CUE_POINTS

    def test_changed_audio_gives_no_cues_though_the_old_row_remains(self, conn: Conn) -> None:
        file_id, old_audio = seed.cued_master(conn)
        _set_file_column(conn, file_id, "audio_hash", seed.audio_hash())

        assert _cues_of_one_play(conn, file_id) is None
        kept = conn.execute(
            "SELECT count(*) AS n FROM stream_cues WHERE audio_hash = %s", (old_audio,)
        ).fetchone()
        assert kept == {"n": 1}

    def test_files_with_the_same_audio_share_one_row(self, conn: Conn) -> None:
        st = seed.station(conn)
        pl = seed.playlist(conn, st)
        first, audio = seed.cued_master(conn)
        second = seed.mastered_file(conn, audio_hash=audio)
        seed.matched_play(conn, pl, at("08:00"), first)
        seed.matched_play(conn, pl, at("09:00"), second)

        items = get_day(conn, st)

        assert [(i.file.file_id, i.file.cues) if i.file else None for i in items] == [
            (first, CUE_POINTS),
            (second, CUE_POINTS),
        ]
        rows = conn.execute(
            "SELECT count(*) AS n FROM stream_cues WHERE audio_hash = %s", (audio,)
        ).fetchone()
        assert rows == {"n": 1}

    def test_failed_analysis_row_is_returned_as_normal_cues(self, conn: Conn) -> None:
        file = only_file(
            conn, _file_with_cues(conn, analysis_failed=True, loudness_lufs=None, gain_db=-8.0)
        )
        assert file is not None
        assert file.cues == CuePoints(
            cue_in_ms=1_000,
            cue_out_ms=181_000,
            fade_in_ms=2_000,
            fade_out_ms=5_000,
            start_next_ms=4_000,
            gain_db=-8.0,
        )

    @pytest.mark.parametrize(
        "row",
        [
            {"cue_out_ms": 1_000},
            {"start_next_ms": 180_000},
            {"fade_in_ms": -1},
            {"gain_db": float("nan")},
        ],
        ids=["cue-out-not-after-cue-in", "start-next-past-span", "negative-fade", "nan-gain"],
    )
    def test_invalid_cue_row_gives_no_cues_and_a_warning(
        self, conn: Conn, row: dict[str, object]
    ) -> None:
        st = _file_with_cues(conn, **row)

        with capture_logs() as logs:
            items = get_day(conn, st)

        [item] = items
        assert item.file is not None
        assert item.file.cues is None
        [warning] = warnings(logs, "schedule_cues_invalid")
        assert str(warning["event_id"]) == str(item.event_id)
        assert str(warning["file_id"]) == str(item.file.file_id)
        assert warning["error"]

    def test_cues_are_those_of_the_resolved_file_audio(self, conn: Conn) -> None:
        st = seed.station(conn)
        work = seed.work(conn)
        recording = seed.work_recording(conn, work)
        matched_audio, master_audio = seed.audio_hash(), seed.audio_hash()
        matched = seed.library_file(conn, recording_id=recording, audio_hash=matched_audio)
        master = seed.library_file(conn, recording_id=recording, audio_hash=master_audio)
        seed.song_master(conn, work, master)
        seed.cue_row(conn, matched_audio, gain_db=-1.5)
        seed.cue_row(conn, master_audio, gain_db=-6.5)
        seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), matched)

        file = only_file(conn, st)
        assert file is not None
        assert file.cues is not None
        assert file.cues.gain_db == -6.5


def test_upserted_cues_are_what_the_reader_returns(conn: Conn) -> None:
    st = seed.station(conn)
    audio = seed.audio_hash()
    file_id = seed.mastered_file(conn, audio_hash=audio)
    seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), file_id)
    analysis = CueAnalysis(
        audio_hash=AudioHash.parse(audio),
        cues=CUE_POINTS,
        loudness_lufs=-13.5,
        analysis_failed=False,
        analyser_version=VERSION,
    )

    PgStreamCueRepository(conn).upsert(analysis)

    file = only_file(conn, st)
    assert file is not None
    assert file.cues == CUE_POINTS


def test_a_day_is_read_with_one_statement(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    st = seed.station(conn, format_name="AC")
    pl = seed.playlist(conn, st)
    work = seed.work(conn)
    master_audio = seed.audio_hash()
    matched = seed.work_file(conn, work)
    master = seed.library_file(
        conn, recording_id=seed.work_recording(conn, work), audio_hash=master_audio
    )
    seed.song_master(conn, work, master)
    seed.cue_row(conn, master_audio)
    seed.matched_play(conn, pl, at("06:00"), matched)
    seed.matched_play(conn, pl, at("06:04"), seed.mastered_file(conn, status="missing"))
    seed.matched_play(conn, pl, at("06:06"), seed.library_file(conn))  # no master
    seed.play(conn, pl, seed.identity(conn, status="pending"), at("06:08"))
    calls: list[object] = []
    original = psycopg.Cursor.execute

    def counting(self: psycopg.Cursor[Any], *args: Any, **kwargs: Any) -> psycopg.Cursor[Any]:
        # Only the reader's connection: a log sink may write on its own connection.
        if self.connection is conn:
            calls.append(args[0] if args else kwargs.get("query"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", counting)

    items = get_day(conn, st)

    assert len(items) == 4
    assert len(calls) == 1

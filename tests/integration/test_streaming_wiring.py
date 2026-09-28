"""Acceptance tests: RepositoryFactory wiring and the domain's DayLoader (spec: Data, PR C)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from functools import partial
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.streaming import (
    CUE_ANALYSER_VERSION,
    DayLoader,
    ItemRef,
    Landing,
    StreamTiming,
    TuneIn,
)
from backend.domain.tune_in import tune_in
from backend.repositories.playable_schedule import PlayableScheduleRepository
from backend.repositories.stream_cues import StreamCueRepository
from backend.services.repository_factory import RepositoryFactory
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import CUE_POINTS, DAY, Conn, at


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


class TestWiring:
    def test_factory_has_a_streaming_group_of_ports(self, conn: Conn) -> None:
        repos = RepositoryFactory(conn)

        assert isinstance(repos.streaming.cues, StreamCueRepository)
        assert isinstance(repos.streaming.schedule, PlayableScheduleRepository)

    def test_factory_reader_judges_freshness_by_the_current_analyser_version(
        self, conn: Conn
    ) -> None:
        st = seed.station(conn)
        pl = seed.playlist(conn, st)
        current, outdated = seed.library_file(conn), seed.library_file(conn)
        seed.cue_row(conn, current, analyser_version=CUE_ANALYSER_VERSION)
        seed.cue_row(conn, outdated, analyser_version=CUE_ANALYSER_VERSION + 1)
        seed.matched_play(conn, pl, at("08:00"), current)
        seed.matched_play(conn, pl, at("09:00"), outdated)

        items = RepositoryFactory(conn).streaming.schedule.get_day(st, DAY)

        assert [item.file.cues if item.file else "no file" for item in items] == [CUE_POINTS, None]

    @pytest.mark.parametrize(
        ("hms", "landing"),
        [
            # inside the first song: 2 min into a 4 min file
            ("05:59:00", Landing(ItemRef(DAY, 0), 120_000)),
            # inside the master's cued span (210 s), past an unresolved same-second play
            ("06:03:00", Landing(ItemRef(DAY, 2), 110_000)),
            # 5 s left of the cued span (< 10 s minimum): skip the missing file, top of next
            ("06:04:35", Landing(ItemRef(DAY, 4), 0)),
            # anchored on the missing file: top of the next playable song
            ("06:05:10", Landing(ItemRef(DAY, 4), 0)),
        ],
    )
    def test_tune_in_over_the_real_reader_for_one_day(
        self, conn: Conn, hms: str, landing: Landing
    ) -> None:
        st = seed.station(conn, format_name="AC")
        pl = seed.playlist(conn, st)
        seed.matched_play(conn, pl, at("05:57:00"), seed.library_file(conn, duration_ms=240_000))
        unresolved = seed.identity(conn, status="pending", identity_id=UUID(int=0xB))
        seed.play(conn, pl, unresolved, at("06:01:10"))
        work = seed.work(conn)
        master = seed.library_file(
            conn, duration_ms=400_000, recording_id=seed.work_recording(conn, work)
        )
        seed.song_master(conn, work, master)
        seed.cue_row(
            conn, master, cue_in_ms=1_000, cue_out_ms=211_000, analyser_version=CUE_ANALYSER_VERSION
        )
        cued = seed.identity(conn, identity_id=UUID(int=0xC))
        seed.match(conn, cued, seed.work_file(conn, work))
        seed.play(conn, pl, cued, at("06:01:10"))
        seed.matched_play(conn, pl, at("06:05:00"), seed.library_file(conn, status="missing"))
        seed.matched_play(conn, pl, at("06:05:40"), seed.library_file(conn, duration_ms=180_000))

        load_day: DayLoader = partial(RepositoryFactory(conn).streaming.schedule.get_day, st)
        now = datetime.combine(datetime(2026, 3, 14), datetime.strptime(hms, "%H:%M:%S").time())

        assert tune_in(load_day, 1995, now, StreamTiming()) == TuneIn(
            landing, datetime(1995, 3, 14) - datetime(2026, 3, 14)
        )

"""Station liners, PR L1 Task 2: migration 0042, ``PgStationLinerRepository`` and the
station's cadence column, against PostgreSQL.

Requirements: LR1 (per-station cadence, blank = off, 1-1,000, a CHECK on the column), LR3
(stored per station; rename changes only the name; delete removes the row and its edit;
deleting a station deletes its liners), LR4 (each liner has an ``audio_hash``; manual edits
per liner, stored as three columns, all null or all set; autocue sees liners that need
analysis). Pre-flight ruling (a): ``needing_analysis`` gives ``path`` as
``"<station_id>/<file_name>"``, relative to the liners root.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.db.repositories.station_liners import PgStationLinerRepository
from backend.db.repositories.stream_cues import PgStreamCueRepository
from backend.domain.broadcast import BroadcastStation
from backend.domain.library import AudioHash
from backend.domain.liners import (
    DuplicateLinerError,
    LinerCadence,
    LinerCueState,
    StationLiner,
    liner_markers,
)
from backend.domain.streaming import ClipFormat, CueAnalysis, CueCandidate, CuePoints
from tests.integration import stream_seed as seed

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
POINTS = CuePoints(120, 6_900, 0, 400, 300, -3.5)


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    """An autocommit connection: ``migrated_db`` truncates ``stations`` with CASCADE, which
    empties ``station_liners`` through its foreign key."""
    with psycopg.connect(migrated_db, autocommit=True, row_factory=dict_row) as c:
        yield c


def new_hash() -> AudioHash:
    return AudioHash.parse(f"flac-md5:{uuid4().hex}")


def make_liner(
    station_id: UUID,
    *,
    name: str = "WKRP ID",
    audio: AudioHash | None = None,
    fmt: ClipFormat = ClipFormat.FLAC,
    duration_ms: int = 7_000,
    uploaded_at: datetime = T0,
) -> StationLiner:
    liner_id = uuid4()
    return StationLiner(
        id=liner_id,
        station_id=station_id,
        name=name,
        file_name=f"{liner_id}.{fmt}",
        format=fmt,
        audio_hash=audio or new_hash(),
        duration_ms=duration_ms,
        markers=None,
        uploaded_at=uploaded_at,
    )


def analysis(audio: AudioHash, *, failed: bool = False, silent: bool = False) -> CueAnalysis:
    return CueAnalysis(
        audio_hash=audio,
        cues=POINTS,
        loudness_lufs=None,
        analysis_failed=failed,
        analyser_version=1,
        audio_silent=silent,
    )


def liner_count(conn: Conn) -> int:
    found = conn.execute("SELECT count(*) AS n FROM station_liners").fetchone()
    assert found is not None
    return int(found["n"])


def test_add_get_round_trip(conn: Conn) -> None:
    """LR3, LR4: every field reads back as stored, the edit absent."""
    repo = PgStationLinerRepository(conn)
    station_id = seed.station(conn)
    other_station = seed.station(conn)
    liner = make_liner(station_id, fmt=ClipFormat.MP3, duration_ms=8_250)

    repo.add(liner)

    assert repo.get(liner.id) == liner
    assert repo.get(uuid4()) is None
    assert repo.find_by_hash(station_id, liner.audio_hash) == liner
    assert repo.find_by_hash(other_station, liner.audio_hash) is None


def test_same_audio_twice_in_a_station_is_duplicate(conn: Conn) -> None:
    """LR2, LR3: the same audio is one liner per station (UNIQUE station_id, audio_hash)."""
    repo = PgStationLinerRepository(conn)
    station_id = seed.station(conn)
    first = make_liner(station_id)
    repo.add(first)

    with pytest.raises(DuplicateLinerError):
        repo.add(make_liner(station_id, name="Again", audio=first.audio_hash))

    assert liner_count(conn) == 1


def test_same_audio_in_two_stations_is_two_liners(conn: Conn) -> None:
    """LR3 (spec non-goal: no sharing): each station owns its own copy of the audio."""
    repo = PgStationLinerRepository(conn)
    audio = new_hash()
    a = make_liner(seed.station(conn), audio=audio)
    b = make_liner(seed.station(conn), audio=audio)

    repo.add(a)
    repo.add(b)

    assert (repo.get(a.id), repo.get(b.id)) == (a, b)


def test_list_orders_by_name_and_reports_cue_state(conn: Conn) -> None:
    """LR3, LR4, LR5: one station's liners by lower(name), then uploaded_at, each with its
    cue state (no row → analysing; a row → auto; failed; silent; an edit → edited)."""
    repo = PgStationLinerRepository(conn)
    cues = PgStreamCueRepository(conn)
    station_id = seed.station(conn)
    later = make_liner(station_id, name="Jingle", uploaded_at=T0 + timedelta(minutes=5))
    earlier = make_liner(station_id, name="jingle", uploaded_at=T0)
    auto = make_liner(station_id, name="A legal ID")
    edited = make_liner(station_id, name="c ID")
    failed = make_liner(station_id, name="Drop")
    silent = make_liner(station_id, name="empty")
    for liner in (later, earlier, auto, edited, failed, silent):
        repo.add(liner)
    repo.add(make_liner(seed.station(conn), name="B other station"))
    cues.upsert(analysis(auto.audio_hash))
    cues.upsert(analysis(edited.audio_hash))
    cues.upsert(analysis(failed.audio_hash, failed=True))
    cues.upsert(analysis(silent.audio_hash, failed=True, silent=True))
    repo.save_markers(edited.id, liner_markers(0, 5_000, 7_000))

    entries = repo.list_for_station(station_id)

    assert [(e.liner.id, e.cue_state) for e in entries] == [
        (auto.id, LinerCueState.AUTO),
        (edited.id, LinerCueState.EDITED),
        (failed.id, LinerCueState.FAILED),
        (silent.id, LinerCueState.SILENT),
        (earlier.id, LinerCueState.ANALYSING),
        (later.id, LinerCueState.ANALYSING),
    ]
    assert entries[1].liner.markers == liner_markers(0, 5_000, 7_000)


def test_save_markers_then_clear(conn: Conn) -> None:
    """LR4, LR11: a liner's own edit is stored on it and cleared back to none."""
    repo = PgStationLinerRepository(conn)
    liner = make_liner(seed.station(conn))
    repo.add(liner)

    repo.save_markers(liner.id, liner_markers(0, 5_000, 7_000))
    saved = repo.get(liner.id)
    repo.save_markers(liner.id, None)
    cleared = repo.get(liner.id)

    assert saved == replace(liner, markers=liner_markers(0, 5_000, 7_000))
    assert cleared == liner


def test_an_edit_belongs_to_one_liner(conn: Conn) -> None:
    """LR4 (spec "Cues": manual edits are per liner, not per hash): editing station A's liner
    changes neither station B's copy of the same audio nor the library's ``stream_cue_edits``,
    and deleting A's liner leaves no edit anywhere."""
    repo = PgStationLinerRepository(conn)
    audio = new_hash()
    station_b = seed.station(conn)
    mine = make_liner(seed.station(conn), audio=audio)
    theirs = make_liner(station_b, audio=audio)
    repo.add(mine)
    repo.add(theirs)

    repo.save_markers(mine.id, liner_markers(0, 5_000, 7_000))

    assert repo.get(theirs.id) == theirs
    assert [e.cue_state for e in repo.list_for_station(station_b)] == [LinerCueState.ANALYSING]
    assert edit_count(conn, audio) == 0

    repo.delete(mine.id)

    assert repo.get(mine.id) is None
    assert repo.get(theirs.id) == theirs
    assert edit_count(conn, audio) == 0


def edit_count(conn: Conn, audio: AudioHash) -> int:
    """The library's manual edits stored for ``audio`` (``stream_cue_edits``)."""
    found = conn.execute(
        "SELECT count(*) AS n FROM stream_cue_edits WHERE audio_hash = %s", (str(audio),)
    ).fetchone()
    assert found is not None
    return int(found["n"])


@pytest.mark.parametrize(
    "assignment",
    [
        "cue_eom_ms = 5000",  # one column set, the others null
        "cue_start_ms = 0, cue_eom_ms = 5000",  # two of three
        "cue_start_ms = 5000, cue_eom_ms = 5000, cue_stop_ms = 7000",  # start = eom
        "cue_start_ms = 0, cue_eom_ms = 6000, cue_stop_ms = 5000",  # eom > stop
        "cue_start_ms = -1, cue_eom_ms = 5000, cue_stop_ms = 7000",  # start < 0
        "cue_start_ms = 0, cue_eom_ms = 5000, cue_stop_ms = 7001",  # stop > duration
    ],
)
def test_cues_check_refuses_partial_or_bad_markers(conn: Conn, assignment: str) -> None:
    """LR4 (plan note 7): the three cue columns are all null, or 0 <= start < eom <= stop <=
    duration_ms; the table refuses anything else (constraint ``station_liners_cues``)."""
    liner = make_liner(seed.station(conn), duration_ms=7_000)
    PgStationLinerRepository(conn).add(liner)

    with pytest.raises(psycopg.errors.CheckViolation) as raised:
        conn.execute(
            f"UPDATE station_liners SET {assignment} WHERE id = %s",  # noqa: S608
            (liner.id,),
        )

    assert raised.value.diag.constraint_name == "station_liners_cues"


def test_rename_and_delete(conn: Conn) -> None:
    """LR3: rename changes only the name; delete removes the row and its edit with it."""
    repo = PgStationLinerRepository(conn)
    station_id = seed.station(conn)
    liner = make_liner(station_id)
    kept = make_liner(station_id, name="Sweeper")
    repo.add(liner)
    repo.add(kept)
    repo.save_markers(liner.id, liner_markers(0, 5_000, 7_000))

    repo.rename(liner.id, "Legal ID")
    renamed = repo.get(liner.id)
    repo.delete(liner.id)

    assert renamed == replace(liner, name="Legal ID", markers=liner_markers(0, 5_000, 7_000))
    assert repo.get(liner.id) is None
    assert repo.get(kept.id) == kept
    assert liner_count(conn) == 1


def test_station_delete_cascades_liners(conn: Conn) -> None:
    """LR3: deleting a station deletes its liners (ON DELETE CASCADE), not another's."""
    repo = PgStationLinerRepository(conn)
    doomed = seed.station(conn)
    other = seed.station(conn)
    gone = make_liner(doomed)
    kept = make_liner(other)
    repo.add(gone)
    repo.add(kept)

    PgBroadcastStationRepository(conn).delete(doomed)

    assert repo.get(gone.id) is None
    assert repo.get(kept.id) == kept


def test_needing_analysis_lists_unanalysed_liners_only(conn: Conn) -> None:
    """LR4, ruling (a): liners whose audio has no stream_cues row, as candidates keyed by the
    liner id, at ``<station_id>/<file_name>``, with no stored stat."""
    repo = PgStationLinerRepository(conn)
    station_id = seed.station(conn)
    waiting = make_liner(station_id, duration_ms=9_500)
    analysed = make_liner(station_id)
    repo.add(waiting)
    repo.add(analysed)
    PgStreamCueRepository(conn).upsert(analysis(analysed.audio_hash))

    assert repo.needing_analysis() == [
        CueCandidate(
            file_id=waiting.id,
            path=f"{station_id}/{waiting.file_name}",
            audio_hash=waiting.audio_hash,
            duration_ms=9_500,
            file_size=None,
            file_mtime_ns=None,
        )
    ]


def test_cadence_round_trip(conn: Conn) -> None:
    """LR1: the station's cadence is set, read back on the station, and cleared."""
    stations = PgBroadcastStationRepository(conn)
    station_id = seed.station(conn)

    stations.set_liner_cadence(station_id, LinerCadence(3))
    station = stations.get_by_id(station_id)
    assert station is not None and station.liner_cadence == LinerCadence(3)
    assert [s.liner_cadence for s in stations.list_all()] == [LinerCadence(3)]

    stations.set_liner_cadence(station_id, None)
    cleared = stations.get_by_id(station_id)
    assert cleared is not None and cleared.liner_cadence is None


@pytest.mark.parametrize("value", [0, 1001])
def test_cadence_check_refuses_0(conn: Conn, value: int) -> None:
    """LR1: the column itself refuses a cadence outside 1-1,000."""
    station_id = seed.station(conn)

    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "UPDATE stations SET songs_between_liners = %s WHERE id = %s", (value, station_id)
        )


def test_update_station_keeps_cadence(conn: Conn) -> None:
    """LR1: editing the station's details leaves its cadence alone, whatever the station
    object passed to ``update`` carries."""
    stations = PgBroadcastStationRepository(conn)
    station_id = seed.station(conn)
    stations.set_liner_cadence(station_id, LinerCadence(5))

    updated = stations.update(BroadcastStation(station_id, "WKRP", name="WKRP in Cincinnati"))
    read = stations.get_by_id(station_id)

    assert updated.liner_cadence == LinerCadence(5)
    assert read is not None
    assert (read.name, read.liner_cadence) == ("WKRP in Cincinnati", LinerCadence(5))

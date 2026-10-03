"""The streaming settings use cases (PR G1, Tasks 2 and 3; traceability D: T2.1-T2.7, T3.17, T3.27).

Requirements: D10 (the listener limit defaults to 3); D27 (a limit that is not a whole number
>= 1 is refused on save; a bad stored one is reported, PR G validates on save); D42 (the limit
is app-wide and admission is atomic); PG6 (lowering the limit stops no one); D34 and PG1
(streaming is on, off or unavailable, shown read-only; ``STREAM_ENABLED`` stays env-only);
M2 (a sign-off whose file is gone is reported); error handling and H9 (a bad stored sign-off
is reported, never raised: before G1 the generic save could store anything under it).
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from backend.domain.broadcast import BroadcastStation
from backend.domain.streaming import ClipFormat, SignOff
from backend.domain.system import InvalidSettingError
from backend.services.streaming.errors import StationBusyError
from backend.services.streaming.max_sessions import MAX_SESSIONS_KEY, parse_max_sessions
from backend.services.streaming.stream_settings import (
    StreamingState,
    read_stream_settings,
    set_max_sessions,
    streaming_state,
)
from tests.fakes.user_settings import FakeUserSettingRepository
from tests.services.streaming.helpers import DAY, STATION, make_rig, song

SIGN_OFF_KEY = "stream_sign_off"
OTHER_STATION = UUID("0000c0de-0000-4000-8000-000000000002")
CLIP = SignOff(
    file_name="0123456789abcdef.mp3", format=ClipFormat.MP3, span_ms=12_500, name="Good night.mp3"
)


def stored(settings: FakeUserSettingRepository, key: str) -> str | None:
    found = settings.get(key)
    return None if found is None else found.value


def test_with_nothing_set_the_limit_is_3(tmp_path: Path) -> None:
    # T2.1 (D10).
    read = read_stream_settings(FakeUserSettingRepository(), StreamingState.ON, tmp_path)
    assert (read.max_sessions, read.max_sessions_problem) == (3, None)


@pytest.mark.parametrize("value", [0, -3])
def test_setting_the_limit_refuses_less_than_1_and_stores_nothing(value: int) -> None:
    # T2.2 (D27).
    settings = FakeUserSettingRepository({MAX_SESSIONS_KEY: "4"})
    with pytest.raises(InvalidSettingError) as refused:
        set_max_sessions(settings, value)
    assert MAX_SESSIONS_KEY in str(refused.value)
    assert stored(settings, MAX_SESSIONS_KEY) == "4"


@pytest.mark.parametrize("value", [1, 12])
def test_setting_the_limit_stores_it_and_answers_it(value: int, tmp_path: Path) -> None:
    # T2.3 (D27): stored as the whole number the tune-in parses; the stored value is returned.
    settings = FakeUserSettingRepository()
    assert set_max_sessions(settings, value) == value
    assert stored(settings, MAX_SESSIONS_KEY) == str(value)
    read = read_stream_settings(settings, StreamingState.ON, tmp_path)
    assert (read.max_sessions, read.max_sessions_problem) == (value, None)


@pytest.mark.parametrize("raw", ["0", "abc", "2.5"])
def test_a_bad_stored_limit_is_reported_not_raised(raw: str, tmp_path: Path) -> None:
    # T2.4 (D27): a value stored before G1 validated is shown as a problem naming the setting
    # and its value; the page still loads.
    settings = FakeUserSettingRepository({MAX_SESSIONS_KEY: raw})
    read = read_stream_settings(settings, StreamingState.ON, tmp_path)
    assert read.max_sessions is None
    assert read.max_sessions_problem is not None
    assert MAX_SESSIONS_KEY in read.max_sessions_problem
    assert repr(raw) in read.max_sessions_problem


async def test_the_limit_set_here_counts_every_station(tmp_path: Path) -> None:
    # T2.5 (D42; guard on the shim): the value saved here is the one admission parses, and it
    # counts the streams of every station together.
    rig = make_rig(tmp_path)
    rig.stations.create(BroadcastStation(id=OTHER_STATION, call_letters="WXYZ"))
    rig.schedule.set_day(STATION, DAY, [song("06:00:00"), song("06:03:20")])
    rig.schedule.set_day(OTHER_STATION, DAY, [song("06:00:00"), song("06:03:20")])
    set_max_sessions(rig.settings, 2)
    assert parse_max_sessions(stored(rig.settings, MAX_SESSIONS_KEY)) == 2
    await rig.open(call="KIOA")
    await rig.open(call="WXYZ")
    with pytest.raises(StationBusyError):
        await rig.open(call="WXYZ")
    assert rig.service.open_sessions == 2


async def test_lowering_the_limit_stops_no_one(tmp_path: Path) -> None:
    # T2.6 (PG6; guard on the shim): open streams keep playing; only new tune-ins are refused.
    rig = make_rig(tmp_path)
    rig.schedule.set_day(STATION, DAY, [song("06:00:00"), song("06:03:20")])
    for _ in range(3):
        await rig.open()
    set_max_sessions(rig.settings, 1)
    assert rig.service.open_sessions == 3
    assert rig.engines.stopped == []
    with pytest.raises(StationBusyError):
        await rig.open()


@pytest.mark.parametrize(
    ("enabled", "running", "expected"),
    [
        (False, False, StreamingState.OFF),
        (True, False, StreamingState.UNAVAILABLE),
        (True, True, StreamingState.ON),
    ],
)
def test_the_streaming_state_comes_from_the_env_and_the_running_service(
    enabled: bool, running: bool, expected: StreamingState, tmp_path: Path
) -> None:
    # T2.7 (D34, PG1): off when STREAM_ENABLED is false; unavailable when enabled but the
    # engine could not be prepared; on otherwise. Reported as given, never stored.
    state = streaming_state(enabled=enabled, running=running)
    assert state is expected
    settings = FakeUserSettingRepository()
    assert read_stream_settings(settings, state, tmp_path).streaming is expected
    assert settings.list_all() == []


def test_a_sign_off_whose_file_is_gone_is_reported(tmp_path: Path) -> None:
    # T3.17 (M2): the clip is still shown, with a missing-file problem (its state, not its
    # wording: ruling 4); with its file present, no problem.
    settings = FakeUserSettingRepository({SIGN_OFF_KEY: CLIP.to_setting()})
    gone = read_stream_settings(settings, StreamingState.ON, tmp_path)
    assert gone.sign_off == CLIP
    assert gone.sign_off_problem and "missing" in gone.sign_off_problem.lower()

    (tmp_path / CLIP.file_name).write_bytes(b"ID3")
    present = read_stream_settings(settings, StreamingState.ON, tmp_path)
    assert (present.sign_off, present.sign_off_problem) == (CLIP, None)


@pytest.mark.parametrize(
    "value",
    ["not json", '{"file_name": "../escape.mp3", "format": "mp3", "span_ms": 5000, "name": "a"}'],
    ids=["not-json", "bad-file-name"],
)
def test_a_bad_stored_sign_off_is_reported_not_raised(value: str, tmp_path: Path) -> None:
    # T3.27 (M2, H9, error handling; audit SF5): the page still loads; no clip is shown, and
    # a problem says why (its state, not its wording).
    settings = FakeUserSettingRepository({SIGN_OFF_KEY: value})
    read = read_stream_settings(settings, StreamingState.ON, tmp_path)
    assert read.sign_off is None
    assert read.sign_off_problem

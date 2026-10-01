"""Tuning in by call letters in any case (spec D72: "Call letters match in any case"; it
replaces D36's exact match and the deleted locked test ``test_call_letters_are_matched_exactly``;
D11 bookmarks; Errors and edge cases: unknown call letters start nothing)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.services.streaming.errors import StationNotFoundError
from tests.services.streaming.helpers import DAY, STATION, Rig, make_rig, song


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    rig = make_rig(tmp_path)
    rig.schedule.set_day(STATION, DAY, [song("06:00:00"), song("06:03:20")])
    return rig


@pytest.mark.parametrize("call", ["kioa", "Kioa"])
async def test_a_listener_tunes_in_whatever_the_case(rig: Rig, call: str) -> None:
    sid = await rig.open(call=call)
    assert [e.session_id for e in rig.engines.endpoints] == [sid]
    assert rig.service.open_sessions == 1


async def test_unknown_call_letters_still_start_nothing(rig: Rig) -> None:
    with pytest.raises(StationNotFoundError):
        await rig.open(call="KXXX")
    assert rig.engines.endpoints == []
    assert rig.service.open_sessions == 0


async def test_a_bookmark_left_under_one_casing_resumes_under_another(rig: Rig) -> None:
    # D72 with D11: one station, so one bookmark, whatever case the URL used
    rig.schedule.set_day(STATION, DAY, [song("06:00:00"), song("06:30:00"), song("06:33:20")])
    rig.clock.now = datetime(2026, 3, 14, 6, 5)  # a logged gap: lands on 06:30 from the top
    first = await rig.open("car", call="KIOA")
    await rig.item(first, 0)
    rig.started(first, 0)
    rig.clock.advance(seconds=10)
    rig.service.close(first)
    rig.clock.advance(seconds=60)
    second = await rig.open("car", call="kioa")
    # 10 s heard + 60 s away: the resume lands 70 s into the 06:30 song, not the clock's 0 s
    assert (await rig.item(second, 0)).annotations["liq_cue_in"] == "70.000"

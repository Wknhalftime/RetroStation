"""The schedule fake's ``file_cues`` answers what its days carry, so a D85 re-read in the D2
rigs changes nothing (spec: D85; project rule: fakes implement the repository ABCs)."""

from __future__ import annotations

from uuid import uuid4

from backend.domain.streaming import CuePoints
from tests.fakes.playable_schedule import FakePlayableScheduleRepository
from tests.services.streaming.schedule import DAY, STATION, song

CUES = CuePoints(1_500, 181_000, 2_000, 5_000, 4_500, -6.2)


def test_the_fake_answers_the_cues_its_days_carry() -> None:
    schedule = FakePlayableScheduleRepository()
    cued, uncued = song("06:00:00", cues=CUES), song("06:03:20")
    schedule.set_day(STATION, DAY, [cued, uncued])
    assert cued.file is not None and uncued.file is not None
    answers = [schedule.file_cues(f) for f in (cued.file.file_id, uncued.file.file_id, uuid4())]
    assert answers == [CUES, None, None]

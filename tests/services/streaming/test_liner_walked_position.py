"""Station liners, follow-up to PR L2 (#171) final review M3 and the T11 D80 note: during a
liner, a reconnect or a bookmark is placed from the next song the stream walked to, at its
top, not by spending the song before the liner's sent span against the day as read at
tune-in.

Requirements:
- LR9 (timeless): during a liner a reconnect lands, and a bookmark points, at the top of the
  next song. "The next song" is the one the stream itself walked to after the song before
  the liner (``LinerSlot.next_song``, which the next seq then plays).
- M3: a D85 re-read that shrank the song before the liner (cues stored mid-session) must not
  move that point back into the song's tail.
- D80 (T11 note): a landing whose cues were dropped plays to the file's end, past its
  cue-out; with cadence 1 a liner follows it, and the point must not move into the next song
  by the dropped stretch. D83: a D80 landing cannot be produced end to end, so this is pinned
  on ``live_position`` with a hand-built session.

The end-to-end rig is the liner rig (cadence 2, songs ``s0``..``s9`` of 200 s with no cues;
a clock tune-in lands 60 s into ``s0``), rewired so the D85 re-read answers from
``AnalysedSince``.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from backend.domain.liners import PlayableLiner
from backend.domain.streaming import CuePoints, ItemRef, Landing, ScheduleItem
from backend.services.streaming.live_position import live_position
from backend.services.streaming.service import StreamRepos
from backend.services.streaming.sessions import Assigned, Committed, LinerSlot, StreamSession
from tests.services.streaming.events_rig import STEADY_START
from tests.services.streaming.liner_reads_rig import AnalysedSince, rewired
from tests.services.streaming.liner_rig import LINER_B, LinerRig, make_liner_rig, songs
from tests.services.streaming.schedule import DAY, song

KEY = "car"
SHORTENED = CuePoints(0, 180_000, 0, 4_000, 0, -3.0)
"""s1's cues, stored mid-session: its span is 180 s, 20 s short of the 200 s read at tune-in
(more than D9's 4 + 3 s minimum, so a walk by the old span stops inside s1)."""

LANDING_CUES = CuePoints(0, 190_000, 0, 4_000, 0, -3.0)
"""s0's cues in the D80 test: 185 s in leaves 5 s, short of D9's 4 + 3 s minimum."""


def analysed_rig(tmp_path: Path) -> LinerRig:
    """The liner rig whose D85 re-read finds ``SHORTENED`` stored for s1's file."""
    rig = make_liner_rig(tmp_path)
    s1 = rig.day[1].file
    assert s1 is not None
    general = StreamRepos(
        stations=rig.stations, settings=rig.settings, schedule=rig.schedule, liners=rig.liners
    )
    short = StreamRepos(
        stations=rig.stations,
        settings=rig.settings,
        schedule=AnalysedSince(rig.schedule, {s1.file_id: SHORTENED}),
        liners=rig.liners,
    )
    return rewired(rig, tmp_path, general=general, short=short)


async def landed(rig: LinerRig, sid: str) -> tuple[str, str]:
    """Where ``sid`` landed: its seq 0's song and ``liq_cue_in``."""
    payload = await rig.item(sid, 0)
    return rig.label(payload), payload.annotations["liq_cue_in"]


@pytest.mark.parametrize("leave", ["reconnect", "bookmark"])
async def test_a_liner_after_a_shrunk_song_resumes_at_the_next_songs_top(
    tmp_path: Path, leave: str
) -> None:
    """M3: s1 is sent with the 180 s span re-read mid-session (D85), so the liner follows at
    its end. 3 s into the liner, a reconnect or a resume lands at the top of s2. Spending
    s1's sent span against the day as read at tune-in (200 s) gives 180 s into s1, its tail
    again; a clock tune-in, 173 s into s1."""
    rig = analysed_rig(tmp_path)
    old = await rig.open(KEY)
    assert await rig.hand_out(old, 4) == ["s0", "s1", "b", "s2"]
    assert (await rig.item(old, 1)).annotations["liq_cue_out"] == "180.000"
    rig.started(old, 0)
    rig.elapse(140)  # the rest of s0 from its 60 s landing
    rig.started(old, 1)
    rig.elapse(170)  # the liner starts 10 s before s1's sent end
    rig.started(old, 2)
    rig.elapse(3)
    if leave == "bookmark":
        rig.service.close(old)

    again = await rig.open(KEY)

    assert await landed(rig, again) == ("s2", "0.000")


def test_live_position_during_a_liner_is_the_next_songs_top() -> None:
    """D80 with cadence 1 (pinned on ``live_position``, D83): the landing ``s0``, 185 s in,
    lost its 190 s cue-out to D80 and was sent to its file's end (15 s; the day's cued span
    has 5 s left). During the liner after it, the live position is ``next_song`` at its top
    with nothing heard, the play found there, not s0 plus its 15 s (10 s into s1)."""
    day: list[ScheduleItem] = [song("06:00:00", cues=LANDING_CUES), *songs(3)[1:]]
    session = StreamSession(load_day=lambda _: day, opened_at=STEADY_START, bookmark_key=None)
    session.clock_offset = timedelta(0)
    landing = Assigned(ItemRef(DAY, 0), day[0], 185_000)
    slot = LinerSlot(
        liner=PlayableLiner(LINER_B, f"{LINER_B}.wav", CuePoints(0, 7_000, 0, 0, 0, -3.5)),
        path=Path("liners") / f"{LINER_B}.wav",
        after=landing,
        after_span_ms=15_000,
        next_song=Assigned(ItemRef(DAY, 1), day[1], 0),
    )
    session.assigned = {0: landing, 1: slot}
    started = STEADY_START + timedelta(seconds=15)
    session.committed = Committed(1, slot, started)

    position = live_position("sid", session, started + timedelta(seconds=3))

    assert position is not None
    assert (position.started, position.event_id, position.heard) == (
        Landing(ItemRef(DAY, 1), 0),
        day[1].event_id,
        timedelta(0),
    )

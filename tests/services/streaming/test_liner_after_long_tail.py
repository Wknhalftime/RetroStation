"""A liner next means a 1 s fade after a long tail (EOM ending rules, stage 1, Task 7).

Lance (2026-11-07): when a liner follows, the old song gets out of the way quickly (1 s);
song to song after a sustained ending, 2 s. The reader gives a long-tail song its song-to-song
cues and its ending (Task 5); the stream service shortens the fade when the session's cadence
makes a liner due right after that song. A manual edit is never touched.

The liner rig (``liner_rig.py``) plays liners every 2 songs: s0 (the landing), s1, a liner.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from backend.domain.endings import EndingType, Follows, SongEnding, ruled_cues
from backend.domain.streaming import CuePoints, ScheduleItem
from backend.services.streaming.sessions import LinerSlot
from tests.services.streaming.liner_rig import LinerRig, make_liner_rig, songs

STORED = CuePoints(
    cue_in_ms=0,
    cue_out_ms=200_000,
    fade_in_ms=0,
    fade_out_ms=4_000,
    start_next_ms=5_000,
    gain_db=-6.0,
)
LONG_TAIL = SongEnding(EndingType.LONG_TAIL, 190_500, 200_000)


def with_cues(item: ScheduleItem, cues: CuePoints, ending: SongEnding | None) -> ScheduleItem:
    assert item.file is not None
    return replace(item, file=replace(item.file, cues=cues, ending=ending))


def rig_with(
    tmp_path: Path,
    cues: CuePoints,
    ending: SongEnding | None,
    *,
    cadence: int | None = 2,
    index: int = 1,
) -> LinerRig:
    """The liner rig whose song ``index`` (s1 by default) plays ``cues`` with ``ending``, as
    the reader would hand them over."""
    rig = make_liner_rig(tmp_path, cadence=cadence)
    day = songs(10)
    day[index] = with_cues(day[index], cues, ending)
    rig.set_day(day)
    return rig


def rig_with_s1(
    tmp_path: Path, cues: CuePoints, ending: SongEnding | None, cadence: int | None = 2
) -> LinerRig:
    return rig_with(tmp_path, cues, ending, cadence=cadence)


async def test_long_tail_before_due_liner_fades_one_second(tmp_path: Path) -> None:
    rig = rig_with_s1(tmp_path, ruled_cues(STORED, LONG_TAIL, Follows.SONG), LONG_TAIL)
    sid = await rig.open()
    labels = await rig.hand_out(sid, 3)
    assert labels[:2] == ["s0", "s1"]
    sent = (await rig.item(sid, 1)).annotations
    assert (sent["liq_cue_out"], sent["liq_fade_out"], sent["sn_rem"]) == (
        "191.500",
        "1.000",
        "1.000",
    )


async def test_long_tail_with_no_liner_due_fades_two_seconds(tmp_path: Path) -> None:
    rig = rig_with_s1(
        tmp_path, ruled_cues(STORED, LONG_TAIL, Follows.SONG), LONG_TAIL, cadence=None
    )
    sid = await rig.open()
    await rig.hand_out(sid, 2)
    sent = (await rig.item(sid, 1)).annotations
    assert (sent["liq_cue_out"], sent["liq_fade_out"], sent["sn_rem"]) == (
        "192.500",
        "2.000",
        "2.000",
    )


async def test_other_ending_before_liner_keeps_one_second(tmp_path: Path) -> None:
    other = SongEnding(EndingType.OTHER, None, 200_000)
    rig = rig_with_s1(tmp_path, ruled_cues(STORED, other, Follows.SONG), other)
    sid = await rig.open()
    await rig.hand_out(sid, 3)
    sent = (await rig.item(sid, 1)).annotations
    assert (sent["liq_cue_out"], sent["liq_fade_out"], sent["sn_rem"]) == (
        "196.000",
        "1.000",
        "1.000",
    )


async def test_edited_song_before_liner_is_unchanged(tmp_path: Path) -> None:
    edited = CuePoints(0, 188_000, 0, 2_000, 3_000, -6.0)  # an edit: the reader sets no ending
    rig = rig_with_s1(tmp_path, edited, None)
    sid = await rig.open()
    await rig.hand_out(sid, 3)
    sent = (await rig.item(sid, 1)).annotations
    assert (sent["liq_cue_out"], sent["liq_fade_out"], sent["sn_rem"]) == (
        "188.000",
        "2.000",
        "3.000",
    )


async def test_long_tail_right_after_a_liner_fades_two_seconds(tmp_path: Path) -> None:
    """Cadence 2: s0, s1, a liner, s2, s3, a liner. No liner is due right after s2."""
    rig = rig_with(tmp_path, ruled_cues(STORED, LONG_TAIL, Follows.SONG), LONG_TAIL, index=2)
    sid = await rig.open()
    labels = await rig.hand_out(sid, 4)
    assert labels[3] == "s2"
    sent = (await rig.item(sid, 3)).annotations
    assert (sent["liq_cue_out"], sent["liq_fade_out"], sent["sn_rem"]) == (
        "192.500",
        "2.000",
        "2.000",
    )


async def test_landing_before_due_liner_fades_one_second(tmp_path: Path) -> None:
    """Cadence 1: the landing is song 1, so a liner is due right after it."""
    rig = rig_with(
        tmp_path, ruled_cues(STORED, LONG_TAIL, Follows.SONG), LONG_TAIL, cadence=1, index=0
    )
    sid = await rig.open()
    sent = (await rig.item(sid, 0)).annotations
    assert (sent["liq_fade_out"], sent["sn_rem"]) == ("1.000", "1.000")


async def test_liner_slot_span_matches_sent_song(tmp_path: Path) -> None:
    """Review Focus 5: the liner after a shortened long tail is placed from the span the song
    was sent with (191.5 s), not its song-to-song span (192.5 s). Read from the session's
    stored slot, as the liner read-error tests do."""
    rig = rig_with_s1(tmp_path, ruled_cues(STORED, LONG_TAIL, Follows.SONG), LONG_TAIL)
    sid = await rig.open()
    await rig.hand_out(sid, 3)
    slot = rig.service._sessions[sid].assigned[2]
    assert isinstance(slot, LinerSlot)
    assert slot.after_span_ms == 191_500

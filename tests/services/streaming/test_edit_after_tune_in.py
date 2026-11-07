"""An edit saved after tune-in beats the ending it replaced (EOM ending rules, final review C1).

The day read carries a long tail's ending on ``PlayableFile.ending``; the D85 per-song re-read
brings in cues only. When Lance saves an edit while a listener is tuned in, the re-read must
not leave the old ending on the file, or the liner fade would shorten his edit. The spec's
anti-goal: "A manual edit is overwritten or ignored."
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

from backend.domain.endings import EndingType, Follows, SongEnding, ruled_cues
from backend.domain.streaming import CuePoints, PlayableFile, StreamTiming
from tests.services.streaming.liner_rig import make_liner_rig, songs
from tests.services.streaming.schedule import DAY, STATION

STORED = CuePoints(
    cue_in_ms=0,
    cue_out_ms=200_000,
    fade_in_ms=0,
    fade_out_ms=4_000,
    start_next_ms=5_000,
    gain_db=-6.0,
)
LONG_TAIL = SongEnding(EndingType.LONG_TAIL, 190_500, 200_000)
SONG_CUES = ruled_cues(STORED, LONG_TAIL, Follows.SONG)  # EOM 195 s, Stop 197 s
EDITED = CuePoints(0, 198_500, 0, 3_500, 3_500, -6.0)
"""Lance drags Stop to 198.5 s on the editor's opening markers: EOM and fade-at stay 195 s,
which is the rule's own shape (fade from EOM to Stop)."""


def test_a_changed_reread_drops_the_ending() -> None:
    file = PlayableFile(uuid.uuid4(), "x.flac", 205_000, SONG_CUES, LONG_TAIL)
    fresh = file.with_stored_cues(EDITED, StreamTiming())
    assert fresh.cues == EDITED
    assert fresh.ending is None


def test_an_unchanged_reread_keeps_the_ending() -> None:
    file = PlayableFile(uuid.uuid4(), "x.flac", 205_000, SONG_CUES, LONG_TAIL)
    assert file.with_stored_cues(SONG_CUES, StreamTiming()) == file


async def test_edit_saved_after_tune_in_is_not_shortened_before_a_liner(tmp_path: Path) -> None:
    rig = make_liner_rig(tmp_path, cadence=2)
    day = songs(10)
    assert day[1].file is not None
    day[1] = replace(day[1], file=replace(day[1].file, cues=SONG_CUES, ending=LONG_TAIL))
    rig.set_day(day)
    sid = await rig.open()  # the session's day memo has the long tail and its ending
    # Lance saves the edit: the reader now answers the edit, and no ending, for s1.
    edited_day = list(day)
    assert day[1].file is not None
    edited_day[1] = replace(day[1], file=replace(day[1].file, cues=EDITED, ending=None))
    rig.schedule.set_day(STATION, DAY, edited_day)
    await rig.hand_out(sid, 3)
    sent = (await rig.item(sid, 1)).annotations
    assert (sent["liq_cue_out"], sent["liq_fade_out"], sent["sn_rem"]) == (
        "198.500",
        "3.500",
        "3.500",
    )

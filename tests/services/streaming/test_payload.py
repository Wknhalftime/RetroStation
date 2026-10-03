"""What the backend sends Liquidsoap for one item (spec: contract annotations; Engine "always
send a valid liq_cue_out greater than liq_cue_in"; D23 no-cues songs "sn_rem = 0 and
liq_amplify = 0.0 dB, sent explicitly. cue_out = duration_ms"; "Paths are sent with the
\\\\?\\ prefix"; D26 the final hook)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from backend.domain.streaming import (
    CuePoints,
    InvalidStreamValueError,
    PlayableFile,
    ScheduleItem,
    StreamTiming,
)
from backend.playout.liquidsoap_process import long_path
from backend.services.streaming.payload import FinalClip, final_payload, item_payload
from tests.services.streaming.schedule import at

CUES = CuePoints(
    cue_in_ms=1_500,
    cue_out_ms=181_000,
    fade_in_ms=2_000,
    fade_out_ms=5_000,
    start_next_ms=4_500,
    gain_db=-6.2,
)
TIMING = StreamTiming()


def item_with(cues: CuePoints | None, duration_ms: int | None = 200_000) -> ScheduleItem:
    return ScheduleItem(
        event_id=uuid4(),
        logged_at=at("06:00:00"),
        title="Fernando",
        artist="ABBA",
        file=PlayableFile(
            file_id=uuid4(), path="D:/Music/abba.flac", duration_ms=duration_ms, cues=cues
        ),
    )


def test_a_cued_item_sends_its_cue_points_in_seconds() -> None:
    assert item_payload(7, item_with(CUES), 0, TIMING).annotations == {
        "item_seq": "7",
        "liq_cue_in": "1.500",
        "liq_cue_out": "181.000",
        "liq_fade_in": "2.000",
        "liq_fade_out": "5.000",
        "sn_rem": "4.500",
        "liq_amplify": "-6.2 dB",
        "title": "Fernando",
        "artist": "ABBA",
    }


def test_the_landing_offset_is_added_to_cue_in() -> None:
    sent = item_payload(0, item_with(CUES), 60_000, TIMING).annotations
    assert (sent["liq_cue_in"], sent["liq_cue_out"]) == ("61.500", "181.000")


def test_times_are_exact_to_the_millisecond() -> None:
    assert item_payload(0, item_with(CUES), 60_001, TIMING).annotations["liq_cue_in"] == "61.501"


def test_a_song_without_cues_plays_to_its_end_with_no_overlap_and_no_gain_change() -> None:
    assert item_payload(3, item_with(None), 0, TIMING).annotations == {
        "item_seq": "3",
        "liq_cue_in": "0.000",
        "liq_cue_out": "200.000",
        "liq_fade_in": "3.000",
        "liq_fade_out": "4.000",
        "sn_rem": "0.000",
        "liq_amplify": "0.0 dB",
        "title": "Fernando",
        "artist": "ABBA",
    }


@pytest.mark.parametrize(
    ("cues", "offset_ms"),
    [(None, 0), (None, 100_000), (None, 190_000), (CUES, 0), (CUES, 169_500)],
)
def test_cue_out_is_always_after_cue_in(cues: CuePoints | None, offset_ms: int) -> None:
    # The largest offsets are the last landings D9 allows: exactly the minimum span is left.
    sent = item_payload(0, item_with(cues), offset_ms, TIMING).annotations
    assert float(sent["liq_cue_out"]) > float(sent["liq_cue_in"])


def test_the_path_is_sent_in_long_path_form() -> None:
    sent = item_payload(0, item_with(CUES), 0, TIMING)
    assert sent.path == long_path(Path("D:/Music/abba.flac"))


def test_an_unresolved_play_is_rejected() -> None:
    unresolved = ScheduleItem(
        event_id=uuid4(), logged_at=at("06:00:00"), title="T", artist="A", file=None
    )
    with pytest.raises(InvalidStreamValueError, match="item_payload"):
        item_payload(0, unresolved, 0, TIMING)


def test_a_final_clip_is_marked_final_and_plays_whole() -> None:
    # D26: only the hook is PR D's; the clip's fades and titles are PR G's.
    clip = FinalClip(path=Path("assets/signoff.flac"), span_ms=4_000)
    sent = final_payload(9, clip)
    assert sent.path == long_path(Path("assets/signoff.flac"))
    assert (sent.annotations["final"], sent.annotations["liq_cue_out"]) == ("true", "4.000")


def test_a_final_clip_needs_a_positive_span() -> None:
    with pytest.raises(InvalidStreamValueError, match="FinalClip.span_ms"):
        FinalClip(path=Path("clip.flac"), span_ms=0)

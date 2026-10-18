"""No song fades in (spec D112, Lance 2026-10-17: "all fade in are removed"; amends D9, D23,
D52). Every item is sent with ``liq_fade_in`` "0.000" — a measured song, a song with no
``stream_cues`` row and a failed-analysis fallback row alike — and D9's minimum span counts
no fade-in: ``fade_out + 3 s``, 7 s for a no-cues song at the default 4 s fade-out."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from backend.domain.library import AudioHash
from backend.domain.streaming import (
    CueCandidate,
    CuePoints,
    PlayableFile,
    ScheduleItem,
    StreamTiming,
)
from backend.services.streaming.autocue import Unusable, autocue_points
from backend.services.streaming.cue_record import Autocued, record_analysis
from backend.services.streaming.payload import item_payload, landing_payload
from tests.fakes.stream_cues import FakeStreamCueRepository
from tests.services.streaming.helpers import DAY, STATION, song
from tests.services.streaming.no_cue_rig import make_reporting_rig
from tests.services.streaming.test_autocue import BEHEMOTH
from tests.services.streaming.test_payload import item_with

TIMING = StreamTiming()
AUDIO = AudioHash.parse("flac-md5:" + "fedcba9876543210" * 2)
FILE = uuid4()


def measured(metadata: dict[str, str]) -> CuePoints:
    points = autocue_points(metadata)
    assert not isinstance(points, Unusable)
    return points


MEASURED = measured(BEHEMOTH)
"""Real autocue output whose measured fade-in is 0.2 s."""

LEGACY_FALLBACK = CuePoints(
    cue_in_ms=0,
    cue_out_ms=200_000,
    fade_in_ms=3_000,
    fade_out_ms=4_000,
    start_next_ms=0,
    gain_db=-8.0,
)
"""A D52 fallback row stored before D112, still in stream_cues with its 3 s fade-in."""


def fallback_row_for(duration_ms: int) -> CuePoints:
    store = FakeStreamCueRepository()
    store.file_hashes[FILE] = AUDIO
    candidate = CueCandidate(
        file_id=FILE,
        path="D:/Music/a.flac",
        audio_hash=AUDIO,
        duration_ms=duration_ms,
        file_size=1,
        file_mtime_ns=2,
    )
    record_analysis(store, TIMING, Autocued(candidate, {}))
    return store.analyses[AUDIO].cues


def test_the_measured_fade_in_is_stored_as_measured() -> None:
    """Precondition for the tests below: the analysis really holds a fade-in to ignore."""
    assert MEASURED.fade_in_ms == 200


@pytest.mark.parametrize(
    "cues",
    [MEASURED, LEGACY_FALLBACK, None],
    ids=["measured", "fallback stored before D112", "no cues (D23)"],
)
def test_every_song_is_sent_with_no_fade_in(cues: CuePoints | None) -> None:
    sent = item_payload(0, item_with(cues), 0, TIMING).annotations
    assert sent["liq_fade_in"] == "0.000"


def test_a_failed_analysis_stores_a_fallback_row_with_no_fade_in() -> None:
    """D52 as amended: the default fade-out, but no fade-in."""
    cues = fallback_row_for(200_000)
    assert (cues.fade_in_ms, cues.fade_out_ms) == (0, TIMING.default_fade_out_ms)


def test_a_landing_that_drops_its_cues_is_sent_with_no_fade_in() -> None:
    """D80: a tail short of D9's minimum plays with D23's values, which have no fade-in."""
    span = MEASURED.cue_out_ms - MEASURED.cue_in_ms
    short_tail = span - (MEASURED.fade_out_ms + 3_000) + 1
    sent = landing_payload(0, item_with(MEASURED), short_tail, TIMING).annotations
    assert sent["sn_rem"] == "0.000"  # D23's values, not the song's own cues
    assert sent["liq_fade_in"] == "0.000"


def no_cues(duration_ms: int) -> PlayableFile:
    return PlayableFile(file_id=uuid4(), path="D:/a.flac", duration_ms=duration_ms, cues=None)


def test_a_song_without_cues_needs_seven_seconds() -> None:
    """D9 as amended: the default 4 s fade-out plus 3 s."""
    assert no_cues(240_000).min_span_ms(TIMING) == 7_000
    assert no_cues(7_000).is_playable(TIMING)
    assert not no_cues(6_999).is_playable(TIMING)


@pytest.mark.parametrize("fade_in_ms", [0, 200, 3_000])
def test_a_stored_fade_in_does_not_count_towards_the_minimum_span(fade_in_ms: int) -> None:
    """D9 as amended: a cued song needs its own fade-out plus 3 s, whatever fade-in is stored."""
    cues = CuePoints(
        cue_in_ms=1_000,
        cue_out_ms=9_000,
        fade_in_ms=fade_in_ms,
        fade_out_ms=5_000,
        start_next_ms=0,
        gain_db=0.0,
    )
    cued = PlayableFile(file_id=uuid4(), path="D:/a.flac", duration_ms=10_000, cues=cues)
    assert cued.min_span_ms(TIMING) == 8_000
    assert cued.is_playable(TIMING)
    assert cued.tail_fits(0, TIMING)
    assert not cued.tail_fits(1, TIMING)


def file_of(item: ScheduleItem) -> UUID:
    assert item.file is not None
    return item.file.file_id


async def test_every_item_the_service_hands_out_is_sent_with_no_fade_in(tmp_path: Path) -> None:
    """D112 at the stream service: the landing (cues read at tune-in), a song with no cues
    (D23) and a song whose old fallback row is re-read just before it is queued (D85) all
    start at full level. The gains show each item was sent with the cues meant for it."""
    reporting = make_reporting_rig(tmp_path)
    items = [song("06:00:00", cues=MEASURED), song("06:03:20"), song("06:06:40")]
    reporting.rig.schedule.set_day(STATION, DAY, items)
    reporting.schedule.stored[file_of(items[2])] = LEGACY_FALLBACK
    sid = await reporting.rig.open()
    sent = [(await reporting.rig.item(sid, seq)).annotations for seq in range(3)]
    await reporting.reporter.drained()
    assert [(a["liq_amplify"], a["liq_fade_in"]) for a in sent] == [
        ("-11.9 dB", "0.000"),
        ("0.0 dB", "0.000"),
        ("-8.0 dB", "0.000"),
    ]

"""The player's side of songs without cues (spec: D78, "A song that reaches the player without
cues plays with D23's values and logs a warning"; D79, "The player reports a no-cue song to
the cue owner ... The consumer only asks 'is there data?' and reports when there is none";
D81, no long-song rule; D82: one warning and one report per file per app run, repeats
at debug; D87(c): that memory holds at most 4096 files, the oldest forgotten)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from backend.domain.streaming import CuePoints, ScheduleItem
from backend.services.streaming.service import StreamServiceConfig
from tests.services.streaming.helpers import BACKEND, DAY, STATION, song
from tests.services.streaming.no_cue_rig import CueOwner, ReportingRig, make_reporting_rig

type LogEvent = dict[str, object]

ANALYSED = CuePoints(
    cue_in_ms=1_500,
    cue_out_ms=181_000,
    fade_in_ms=2_000,
    fade_out_ms=5_000,
    start_next_ms=4_500,
    gain_db=-6.2,
)
FAILED_ROW = CuePoints(
    cue_in_ms=0,
    cue_out_ms=200_000,
    fade_in_ms=3_000,
    fade_out_ms=4_000,
    start_next_ms=0,
    gain_db=-8.0,
)
"""D52's fallback row. A failed analysis is still data (D61), so its song has cues."""


@pytest.fixture
def reporting(tmp_path: Path) -> ReportingRig:
    return make_reporting_rig(tmp_path)


def schedule(reporting: ReportingRig, *items: ScheduleItem) -> list[ScheduleItem]:
    """The day the rig plays; NOW is 06:01, so a session lands 60 s into the 06:00 song."""
    reporting.rig.schedule.set_day(STATION, DAY, list(items))
    return list(items)


def file_of(item: ScheduleItem) -> tuple[UUID, str]:
    assert item.file is not None
    return item.file.file_id, item.file.path


def no_cue_events(logs: Sequence[LogEvent]) -> list[LogEvent]:
    return [e for e in logs if e["event"] == "stream_item_no_cues"]


async def test_a_song_without_cues_is_sent_with_d23_values_and_a_warning(
    reporting: ReportingRig,
) -> None:
    """D78 and D23, the landing included: played to its end, no overlap, no gain change; one
    warning names the session, the seq, the play and the file."""
    items = schedule(reporting, song("06:00:00"), song("06:03:20"))
    sid = await reporting.rig.open()
    with capture_logs() as logs:
        sent = [await reporting.rig.item(sid, seq) for seq in (0, 1)]
    played = [(p.annotations["liq_cue_out"], p.annotations["sn_rem"]) for p in sent]
    assert played == [("200.000", "0.000"), ("200.000", "0.000")]
    assert [p.annotations["liq_amplify"] for p in sent] == ["0.0 dB", "0.0 dB"]
    warned = [
        (e["log_level"], e["session_id"], e["seq"], e["event_id"], e["file_id"], e["path"])
        for e in no_cue_events(logs)
    ]
    assert warned == [
        ("warning", sid, seq, str(item.event_id), str(file_of(item)[0]), file_of(item)[1])
        for seq, item in enumerate(items)
    ]


@pytest.mark.parametrize("cues", [ANALYSED, FAILED_ROW], ids=["analysed", "failed row"])
async def test_a_cued_song_is_neither_warned_about_nor_reported(
    reporting: ReportingRig, cues: CuePoints
) -> None:
    """D79: the player only asks "is there data?"; D61: a failed analysis's row is data."""
    schedule(reporting, song("06:00:00", cues=cues), song("06:03:20", cues=cues))
    sid = await reporting.rig.open()
    with capture_logs() as logs:
        for seq in (0, 1):
            await reporting.rig.item(sid, seq)
    await reporting.reporter.drained()
    assert (no_cue_events(logs), reporting.owner.asked) == ([], [])


async def test_every_song_without_cues_is_reported_by_its_file_landing_included(
    reporting: ReportingRig,
) -> None:
    """D79: the landing and later songs alike; D81: a 40-minute song is reported like any
    other, there is no length rule."""
    items = schedule(
        reporting,
        song("06:00:00"),
        song("06:03:20", 2_400),
        song("06:43:20", cues=ANALYSED),
    )
    sid = await reporting.rig.open()
    for seq in (0, 1, 2):
        await reporting.rig.item(sid, seq)
    await reporting.reporter.drained()
    assert reporting.owner.asked == [file_of(items[0])[0], file_of(items[1])[0]]


async def test_a_file_is_warned_about_and_reported_once_per_app_run(
    reporting: ReportingRig,
) -> None:
    """D82: the same song in a later session is a debug line, not a second report."""
    [item] = schedule(reporting, song("06:00:00"))
    with capture_logs() as logs:
        for _ in range(2):
            sid = await reporting.rig.open()
            await reporting.rig.item(sid, 0)
            reporting.rig.service.close(sid)
    await reporting.reporter.drained()
    assert [e["log_level"] for e in no_cue_events(logs)] == ["warning", "debug"]
    assert reporting.owner.asked == [file_of(item)[0]]


async def test_asking_again_for_the_same_item_adds_no_warning_or_report(
    reporting: ReportingRig,
) -> None:
    """The contract: "The same seq always returns the same item"; it is noticed once."""
    schedule(reporting, song("06:00:00"), song("06:03:20"))
    sid = await reporting.rig.open()
    with capture_logs() as logs:
        for _ in range(3):
            await reporting.rig.item(sid, 0)
    await reporting.reporter.drained()
    assert (len(no_cue_events(logs)), len(reporting.owner.asked)) == (1, 1)


async def test_past_the_memory_limit_the_oldest_file_is_noticed_again(tmp_path: Path) -> None:
    """D87(c): the memory is bounded, the oldest file forgotten first; a forgotten file is
    warned about and reported again."""
    reporting = make_reporting_rig(tmp_path, memory=2)
    a, b, c = schedule(reporting, song("06:00:00"), song("06:03:20"), song("06:06:40"))
    first = await reporting.rig.open()
    for seq in (0, 1, 2):
        await reporting.rig.item(first, seq)
    reporting.rig.service.close(first)
    second = await reporting.rig.open()
    await reporting.rig.item(second, 0)  # the clock lands on a again
    await reporting.reporter.drained()
    assert reporting.owner.asked == [file_of(x)[0] for x in (a, b, c, a)]


async def test_a_failing_report_never_fails_the_item(tmp_path: Path) -> None:
    """Never fail playback: the items are answered, and the failure is logged."""
    reporting = make_reporting_rig(tmp_path, owner=CueOwner(fail=True))
    schedule(reporting, song("06:00:00"), song("06:03:20"))
    sid = await reporting.rig.open()
    with capture_logs() as logs:
        sent = [await reporting.rig.item(sid, seq) for seq in (0, 1)]
        await reporting.reporter.drained()
    assert [p.annotations["item_seq"] for p in sent] == ["0", "1"]
    levels = [e["log_level"] for e in logs if e["event"] == "stream_cue_request_failed"]
    assert levels == ["warning", "debug"]


def test_the_memory_limit_must_be_at_least_one(tmp_path: Path) -> None:
    """D87(c)'s bound is a setting of the service; it is validated."""
    with pytest.raises(ValueError, match="StreamServiceConfig.no_cue_memory"):
        StreamServiceConfig(callback_base_url=BACKEND, log_dir=tmp_path, no_cue_memory=0)


async def test_a_report_never_holds_the_item(tmp_path: Path) -> None:
    """D79 with the brief (a report never slows playback; audit SF-2): the items come back
    while the cue owner is still busy with the first report, and the reports arrive after."""
    hold = threading.Event()
    reporting = make_reporting_rig(tmp_path, owner=CueOwner(hold=hold))
    schedule(reporting, song("06:00:00"), song("06:03:20"))
    sid = await reporting.rig.open()
    try:
        sent = [await asyncio.wait_for(reporting.rig.item(sid, seq), 5) for seq in (0, 1)]
        assert reporting.owner.asked == []  # the first report is still held
    finally:
        hold.set()
    await reporting.reporter.drained()
    assert [p.annotations["item_seq"] for p in sent] == ["0", "1"]
    assert len(reporting.owner.asked) == 2


def test_the_memory_holds_4096_files(tmp_path: Path) -> None:
    """D87(c): the warn-once and reported-files memory "remember at most 4096 files"."""
    config = StreamServiceConfig(callback_base_url=BACKEND, log_dir=tmp_path)
    assert config.no_cue_memory == 4096

"""The stream service re-reads each song's cues just before handing it to the engine (spec:
D85; D78/D79/D82 for a song left without cues; ruling (Revision 1): the landing is not
re-read, it was read at tune-in and is on D44's first-audio path; the contract: "The same seq
always returns the same item")."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from structlog.testing import capture_logs

from backend.domain.streaming import CuePoints, ScheduleItem
from backend.services.streaming.cue_reread import CueRereadLimits
from backend.services.streaming.payload import ItemPayload
from tests.services.streaming.helpers import DAY, STATION, song
from tests.services.streaming.no_cue_rig import ReportingRig, make_reporting_rig

type LogEvent = dict[str, object]

ANALYSED = CuePoints(1_500, 181_000, 2_000, 5_000, 4_500, -6.2)


def schedule(reporting: ReportingRig, *items: ScheduleItem) -> list[ScheduleItem]:
    """NOW is 06:01, so a session lands 60 s into the 06:00 song."""
    reporting.rig.schedule.set_day(STATION, DAY, list(items))
    return list(items)


def file_of(item: ScheduleItem) -> UUID:
    assert item.file is not None
    return item.file.file_id


def sound(payload: ItemPayload) -> tuple[str, str, str]:
    """Where it starts, its overlap and its gain: what cues change in what is heard."""
    sent = payload.annotations
    return sent["liq_cue_in"], sent["sn_rem"], sent["liq_amplify"]


def no_cue_events(logs: Sequence[LogEvent]) -> list[LogEvent]:
    return [e for e in logs if e["event"] == "stream_item_no_cues"]


async def test_cues_stored_mid_session_play_on_the_songs_next_play(tmp_path: Path) -> None:
    """D85: the song had no cues at tune-in; the cue owner stored them while the session was
    playing; the song's next play in the same session uses them, and is not warned about."""
    reporting = make_reporting_rig(tmp_path)
    first = song("06:00:00")
    played_again = replace(song("06:06:40"), file=first.file)
    schedule(reporting, first, song("06:03:20"), played_again)
    sid = await reporting.rig.open()
    with capture_logs() as logs:
        await reporting.rig.item(sid, 0)
        reporting.schedule.stored[file_of(first)] = ANALYSED
        await reporting.rig.item(sid, 1)
        again = await reporting.rig.item(sid, 2)
    assert sound(again) == ("1.500", "4.500", "-6.2 dB")
    assert [e["seq"] for e in no_cue_events(logs)] == [0, 1]


async def test_no_stored_row_sends_d23_values_and_reports_the_song(tmp_path: Path) -> None:
    """D85: "A missing row means no cues, so report as before" (D78, D79): a song cued at
    tune-in whose row is gone (an analyser change purged it) plays D23's values."""
    reporting = make_reporting_rig(tmp_path)
    _, later = schedule(reporting, song("06:00:00", cues=ANALYSED), song("06:03:20", cues=ANALYSED))
    reporting.schedule.stored[file_of(later)] = None
    sid = await reporting.rig.open()
    with capture_logs() as logs:
        await reporting.rig.item(sid, 0)
        sent = await reporting.rig.item(sid, 1)
    await reporting.reporter.drained()
    assert sound(sent) == ("0.000", "0.000", "0.0 dB")
    assert [e["log_level"] for e in no_cue_events(logs)] == ["warning"]
    assert reporting.owner.asked == [file_of(later)]


async def test_a_failed_reread_keeps_the_tune_in_values(tmp_path: Path) -> None:
    """D85: "If that read fails, the song uses what was read at tune-in; playback never ...
    fails because of it". The failure is logged."""
    reporting = make_reporting_rig(tmp_path)
    schedule(reporting, song("06:00:00"), song("06:03:20", cues=ANALYSED))
    reporting.schedule.fail = OSError("connection refused")
    sid = await reporting.rig.open()
    with capture_logs() as logs:
        await reporting.rig.item(sid, 0)
        sent = await reporting.rig.item(sid, 1)
    assert sound(sent) == ("1.500", "4.500", "-6.2 dB")
    levels = [e["log_level"] for e in logs if e["event"] == "stream_cue_reread_failed"]
    assert levels == ["warning"]


async def test_a_slow_reread_never_holds_the_item(tmp_path: Path) -> None:
    """D85: "playback never waits on it": past the time limit the song goes out with its
    tune-in values while the read is provably still running (review I1: ``left`` is not set
    yet; a version with no limit, or one reading on the event loop, returns only after the
    read ended)."""
    reporting = make_reporting_rig(tmp_path, reread=CueRereadLimits(timeout_s=0.05))
    schedule(reporting, song("06:00:00"), song("06:03:20", cues=ANALYSED))
    sid = await reporting.rig.open()
    await reporting.rig.item(sid, 0)
    hold = threading.Event()
    reporting.schedule.hold = hold
    try:
        sent = await reporting.rig.item(sid, 1)
        assert not reporting.schedule.left.is_set()  # the read is still inside the fake
        assert reporting.schedule.entered.wait(5)
        assert sound(sent) == ("1.500", "4.500", "-6.2 dB")
    finally:
        hold.set()


async def test_the_landing_is_not_read_again(tmp_path: Path) -> None:
    """Ruling (Revision 1): seq 0 plays what placement read moments before; its offset was
    checked against those values (D9, D80) and it is on D44's first-audio path."""
    reporting = make_reporting_rig(tmp_path)
    [landing] = schedule(reporting, song("06:00:00", cues=ANALYSED))
    reporting.schedule.stored[file_of(landing)] = None
    sid = await reporting.rig.open()
    sent = await reporting.rig.item(sid, 0)
    assert sound(sent) == ("61.500", "4.500", "-6.2 dB")
    assert reporting.schedule.cue_reads == []


async def test_a_repeated_request_returns_the_same_item_whatever_is_stored_since(
    tmp_path: Path,
) -> None:
    """The contract: "The same seq always returns the same item". A repeat request is served
    from the assignment with the values read the first time, even if the stored row has
    changed since (review M3: pinned through the contract, not a call count)."""
    reporting = make_reporting_rig(tmp_path)
    _, later = schedule(reporting, song("06:00:00"), song("06:03:20"))
    reporting.schedule.stored[file_of(later)] = ANALYSED
    sid = await reporting.rig.open()
    await reporting.rig.item(sid, 0)
    first = await reporting.rig.item(sid, 1)
    reporting.schedule.stored[file_of(later)] = None  # the row is gone since
    repeats = [await reporting.rig.item(sid, 1) for _ in range(2)]
    assert repeats == [first, first]
    assert sound(first) == ("1.500", "4.500", "-6.2 dB")


async def test_stored_cues_that_would_leave_the_song_unplayable_are_not_played(
    tmp_path: Path,
) -> None:
    """D86(b), at the service (audit SF-1): a re-read that would leave the song unplayable
    under D9 is ignored; the song keeps its tune-in values, cue-out and gain included, and
    is not reported."""
    reporting = make_reporting_rig(tmp_path)
    _, later = schedule(reporting, song("06:00:00", cues=ANALYSED), song("06:03:20", cues=ANALYSED))
    reporting.schedule.stored[file_of(later)] = CuePoints(0, 9_000, 3_000, 4_000, 0, -3.0)
    sid = await reporting.rig.open()
    await reporting.rig.item(sid, 0)
    sent = await reporting.rig.item(sid, 1)
    await reporting.reporter.drained()
    assert (sound(sent), sent.annotations["liq_cue_out"]) == (
        ("1.500", "4.500", "-6.2 dB"),
        "181.000",
    )
    assert reporting.owner.asked == []

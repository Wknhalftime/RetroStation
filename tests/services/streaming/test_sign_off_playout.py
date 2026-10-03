"""The user's sign-off clip at the end of the schedule (PR G1, Task 4; traceability I:
T4.1-T4.10).

Requirements: D26 (the sign-off clip is the user's own, set in PR G; without one the stream
closes after the last item and the bookmark is cleared); spec "End of schedule"; D78a
("ended" means signed off: the page does not reconnect); D11 (a listener who leaves keeps the
bookmark); D32 (a failed item is flagged); D39 (the day after has no log: the station signs
off); D88 (a read that fails is answered with an error, and the engine retries); D25 and D73
(titles are a song's artist and title only); PG4 (the clip is read when the schedule ends;
a failed read is retried; the user's clip wins over the default); PG5 (a clip that cannot
play still signs off); PG14 (the clip plays whole, at 0 dB, with zero fades and no title;
this closes PR D2's "the clip's fades and titles are PR G's"); PG3 (a bad stored value is
logged and ignored).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

from backend.domain.streaming import (
    ClipFormat,
    EndOfScheduleError,
    SignOff,
    StreamReadError,
)
from backend.playout.liquidsoap_process import long_path
from backend.services.streaming.bookmarks import BookmarkKey
from backend.services.streaming.listener_events import NowPlaying, Status, StatusKind
from backend.services.streaming.payload import FinalClip
from tests.services.streaming.events_rig import drain
from tests.services.streaming.schedule import STATION, YEAR
from tests.services.streaming.sign_off_rig import SignOffRig, make_sign_off_rig, morning

CLIP = SignOff(
    file_name="0123456789abcdef.mp3", format=ClipFormat.MP3, span_ms=12_500, name="Good night.mp3"
)
KEY = BookmarkKey("car", STATION, YEAR)
LAST_SONG_SEQ = 2
CLIP_SEQ = 3


@pytest.fixture
def rig(tmp_path: Path) -> SignOffRig:
    rig = make_sign_off_rig(tmp_path, clip=CLIP)
    morning(rig)
    return rig


async def test_the_users_clip_plays_after_the_last_song_then_the_end(rig: SignOffRig) -> None:
    # T4.1 (D26, PG14): the clip is sent whole, from its folder, at its recorded level, with
    # zero fades, no overlap, no title, marked final; the end marker follows it.
    sid = await rig.open()
    assert await rig.play_to_end(sid) == CLIP_SEQ
    final = await rig.item(sid, CLIP_SEQ)
    assert final.path == rig.clip_path(CLIP)
    assert dict(final.annotations) == {
        "item_seq": str(CLIP_SEQ),
        "liq_cue_in": "0.000",
        "liq_cue_out": "12.500",
        "liq_fade_in": "0.000",
        "liq_fade_out": "0.000",
        "sn_rem": "0.000",
        "liq_amplify": "0.0 dB",
        "title": "",
        "artist": "",
        "final": "true",
    }
    with pytest.raises(EndOfScheduleError):
        await rig.item(sid, CLIP_SEQ + 1)


async def test_with_no_clip_the_stream_ends_after_the_last_song(tmp_path: Path) -> None:
    # T4.2 (D26, D39; guard on the shim): today's end, unchanged.
    rig = make_sign_off_rig(tmp_path)
    morning(rig)
    sid = await rig.open()
    assert await rig.play_to_end(sid) == CLIP_SEQ
    for _ in range(2):  # the same answer every time
        with pytest.raises(EndOfScheduleError):
            await rig.item(sid, CLIP_SEQ)


@pytest.mark.parametrize("change", ["set mid-session", "removed mid-session"])
async def test_the_clip_is_read_when_the_schedule_ends(tmp_path: Path, change: str) -> None:
    # T4.3 (PG4; D40, D44: nothing is added to the tune-in path): a change made while the
    # stream plays is what the end uses; the tune-in itself never reads the clip.
    rig = make_sign_off_rig(tmp_path, clip=None if change == "set mid-session" else CLIP)
    morning(rig)
    sid = await rig.open()
    assert rig.sign_off.sign_off_reads == 0
    if change == "set mid-session":
        rig.set_clip(CLIP)
    else:
        rig.remove_clip()
    assert await rig.play_to_end(sid) == CLIP_SEQ
    if change == "set mid-session":
        assert (await rig.item(sid, CLIP_SEQ)).path == rig.clip_path(CLIP)
    else:
        with pytest.raises(EndOfScheduleError):
            await rig.item(sid, CLIP_SEQ)


async def test_the_clip_starting_signs_the_station_off(rig: SignOffRig) -> None:
    # T4.4 (D26, D78a): once the clip starts, closing tells "ended" and clears the bookmark.
    events = await rig.subscribe("car")
    sid = await rig.open("car")
    await rig.play_to_end(sid)
    await rig.item(sid, CLIP_SEQ)
    rig.started(sid, CLIP_SEQ)
    rig.elapse(12.5)
    rig.service.close(sid)
    told = await drain(events)
    assert told[-1] == Status(StatusKind.ENDED)
    assert rig.bookmarks.get(KEY) is None


async def test_leaving_during_the_last_song_keeps_the_bookmark_when_a_clip_follows(
    rig: SignOffRig,
) -> None:
    # T4.5 (D11, D78a): the clip was sent (prefetched) but never started: the listener left
    # during the last song, so it is "stopped" and the bookmark is kept there.
    items = morning(rig)
    events = await rig.subscribe("car")
    sid = await rig.open("car")
    for seq in range(LAST_SONG_SEQ + 1):
        await rig.item(sid, seq)
        rig.started(sid, seq)
    await rig.item(sid, CLIP_SEQ)
    rig.elapse(30)
    rig.service.close(sid)
    told = await drain(events)
    assert told[-1] == Status(StatusKind.STOPPED)
    kept = rig.bookmarks.get(KEY)
    assert kept is not None
    assert kept.event_id == items[3].event_id


async def test_a_clip_that_cannot_play_still_signs_the_station_off(rig: SignOffRig) -> None:
    # T4.6 (PG5, D32): its file is gone or undecodable; the engine reports it failed. It is
    # flagged, and the station still signs off.
    events = await rig.subscribe("car")
    sid = await rig.open("car")
    await rig.play_to_end(sid)
    await rig.item(sid, CLIP_SEQ)
    with capture_logs() as logs:
        rig.failed(sid, CLIP_SEQ)
    [flagged] = [e for e in logs if e["event"] == "stream_item_failed"]
    assert flagged["path"] == str(rig.folder / CLIP.file_name)
    rig.service.close(sid)
    told = await drain(events)
    assert told[-1] == Status(StatusKind.ENDED)
    assert rig.bookmarks.get(KEY) is None


async def test_a_clip_read_that_fails_is_retried_by_the_engine(rig: SignOffRig) -> None:
    # T4.7 (D88, PG4): the failed read is an error for this request (the engine retries),
    # never "the end"; the retry gets the clip.
    sid = await rig.open()
    for seq in range(LAST_SONG_SEQ + 1):
        await rig.item(sid, seq)
        rig.started(sid, seq)
    assert rig.sign_off.sign_off_reads == 0  # not read per song either (PG4)
    rig.fail_next_read(StreamReadError("stream_sign_off: canceling statement due to lock timeout"))
    with pytest.raises(StreamReadError):
        await rig.item(sid, CLIP_SEQ)
    assert (await rig.item(sid, CLIP_SEQ)).path == rig.clip_path(CLIP)


async def test_the_users_clip_wins_over_the_default_clip(tmp_path: Path) -> None:
    # T4.8 (PG4): the configured default plays only when the user has no clip.
    default = FinalClip(path=Path("assets/default-signoff.flac"), span_ms=4_000)
    for clip, expected in ((CLIP, "user"), (None, "default")):
        rig = make_sign_off_rig(tmp_path / expected, clip=clip, default_clip=default)
        morning(rig)
        sid = await rig.open()
        await rig.play_to_end(sid)
        sent = (await rig.item(sid, CLIP_SEQ)).path
        assert sent == (rig.clip_path(CLIP) if clip else long_path(default.path))


async def test_a_clip_tells_no_title(rig: SignOffRig) -> None:
    # T4.9 (PG14, D25, D73; guard on the shim): the songs' titles are told, the clip's none;
    # ICY keeps the last song's title.
    items = morning(rig)
    events = await rig.subscribe("car")
    sid = await rig.open("car")
    await rig.play_to_end(sid)
    await rig.item(sid, CLIP_SEQ)
    rig.started(sid, CLIP_SEQ)
    rig.elapse(5)
    titles = [e for e in await drain(events) if isinstance(e, NowPlaying)]
    assert titles == [
        NowPlaying(artist=i.artist, title=i.title) for i in (items[0], items[1], items[3])
    ]
    assert rig.service.now_playing(sid) == "Don McLean - Vincent"


async def test_a_bad_stored_sign_off_plays_no_clip_and_is_logged(tmp_path: Path) -> None:
    # T4.10 (PG3, R4): a value that is not a sign-off is ignored at the end, and logged
    # naming the setting.
    rig = make_sign_off_rig(tmp_path)
    morning(rig)
    rig.store_raw('{"file_name": "../../escape.mp3", "format": "mp3"}')
    sid = await rig.open()
    with capture_logs() as logs:
        assert await rig.play_to_end(sid) == CLIP_SEQ
    invalid = [e for e in logs if e["event"] == "stream_setting_invalid"]
    assert invalid and invalid[0]["setting"] == "stream_sign_off"
    with pytest.raises(EndOfScheduleError):
        await rig.item(sid, CLIP_SEQ)

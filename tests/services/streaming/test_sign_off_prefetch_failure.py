"""A sign-off clip that fails while it is still prefetched (PR G1 dev-check fix).

The engine fetches up to two items ahead, so the final clip can be fetched, and reported
failed (its file is gone), while the last song has not started yet. The station must still
sign off (PG5, D26: close tells "ended", D78a, and the bookmark is cleared), but only once
the last song has started, and the freeze watchdog (D31) must follow the last song's span,
not the clip's, so the last song plays to its end. A listener who leaves before the last
song starts keeps the bookmark (D11), as in T4.5.

Clocks: the rig's steady clock is the service's elapsed clock (D47); no sleeps.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.domain.streaming import ClipFormat, SignOff
from backend.services.streaming.bookmarks import BookmarkKey
from backend.services.streaming.listener_events import Status, StatusKind
from tests.services.streaming.events_rig import drain
from tests.services.streaming.schedule import STATION, YEAR, sent_path
from tests.services.streaming.sign_off_rig import SignOffRig, make_sign_off_rig, morning

CLIP = SignOff(
    file_name="fedcba9876543210.mp3", format=ClipFormat.MP3, span_ms=15_000, name="Bye.mp3"
)
KEY = BookmarkKey("car", STATION, YEAR)
LAST_SONG_SEQ = 2
CLIP_SEQ = 3
SONG_S = 200
GRACE_S = 30


@pytest.fixture
def rig(tmp_path: Path) -> SignOffRig:
    return make_sign_off_rig(tmp_path, clip=CLIP)


async def _play_up_to_the_last_song(rig: SignOffRig, sid: str) -> None:
    """Seqs 0 and 1 start and play out; the last song (seq 2) is then assigned."""
    for seq in range(LAST_SONG_SEQ):
        await rig.item(sid, seq)
        rig.started(sid, seq)
        rig.elapse(SONG_S if seq < LAST_SONG_SEQ - 1 else SONG_S - 10)
    await rig.item(sid, LAST_SONG_SEQ)


async def test_a_clip_that_fails_before_the_last_song_lets_the_song_play_out(
    rig: SignOffRig,
) -> None:
    morning(rig)
    events = await rig.subscribe("car")
    sid = await rig.open("car")
    await _play_up_to_the_last_song(rig, sid)
    final = await rig.item(sid, CLIP_SEQ)  # prefetched while seq 1 still plays
    assert final.annotations["final"] == "true"
    rig.failed(sid, CLIP_SEQ)  # its file is gone
    rig.elapse(10)
    rig.started(sid, LAST_SONG_SEQ)  # the last song starts after the clip failed
    rig.elapse(1)
    assert rig.service.now_playing(sid) == "Don McLean - Vincent"
    for _ in range(SONG_S // 20):  # the watchdog runs through the whole last song
        rig.elapse(20)
        assert rig.service.frozen_sessions() == []
    rig.elapse(GRACE_S - 2)  # 1 s short of the song's own span plus the grace
    assert rig.service.frozen_sessions() == []
    rig.elapse(1)  # the watchdog follows the last song's span: it fires exactly there
    assert rig.service.frozen_sessions() == [sid]
    rig.service.close(sid)  # the engine ended the stream after the last song (410)
    told = await drain(events)
    assert told[-1] == Status(StatusKind.ENDED)
    assert rig.bookmarks.get(KEY) is None


async def test_a_clip_that_fails_during_the_last_song_still_signs_off(rig: SignOffRig) -> None:
    morning(rig)
    events = await rig.subscribe("car")
    sid = await rig.open("car")
    await _play_up_to_the_last_song(rig, sid)
    rig.elapse(10)
    rig.started(sid, LAST_SONG_SEQ)
    rig.elapse(5)
    await rig.item(sid, CLIP_SEQ)
    rig.failed(sid, CLIP_SEQ)
    for _ in range(SONG_S // 20 - 1):  # the rest of the last song
        rig.elapse(20)
        assert rig.service.frozen_sessions() == []
    rig.service.close(sid)
    told = await drain(events)
    assert told[-1] == Status(StatusKind.ENDED)
    assert rig.bookmarks.get(KEY) is None


async def test_leaving_before_the_last_song_after_the_clip_failed_keeps_the_bookmark(
    rig: SignOffRig,
) -> None:
    items = morning(rig)
    events = await rig.subscribe("car")
    sid = await rig.open("car")
    await _play_up_to_the_last_song(rig, sid)
    await rig.item(sid, CLIP_SEQ)
    rig.failed(sid, CLIP_SEQ)
    rig.elapse(5)
    rig.service.close(sid)  # the listener left during seq 1
    told = await drain(events)
    assert told[-1] == Status(StatusKind.STOPPED)
    kept = rig.bookmarks.get(KEY)
    assert kept is not None
    assert kept.event_id == items[1].event_id


async def test_a_resume_onto_the_last_song_with_a_failed_clip_signs_off_once_it_starts(
    rig: SignOffRig,
) -> None:
    # The landing (seq 0) is the last song and the clip (seq 1) fails before seq 0 starts.
    # The first visit plays straight through (06:01 lands 60 s into the first song), so it
    # skips the 20 s before 06:07 and is ahead of the station clock: its bookmark is valid.
    items = morning(rig)
    first = await rig.open("car")
    for seq, plays_s in enumerate((SONG_S - 60, SONG_S)):
        await rig.item(first, seq)
        rig.started(first, seq)
        rig.elapse(plays_s)
    await rig.item(first, LAST_SONG_SEQ)
    rig.started(first, LAST_SONG_SEQ)
    rig.elapse(10)
    rig.service.close(first)  # left 10 s into the last song, before the clip was fetched
    assert rig.bookmarks.get(KEY) is not None
    rig.elapse(5)
    events = await rig.subscribe("car")
    sid = await rig.open("car")
    landed = await rig.item(sid, 0)
    assert landed.path == sent_path(items[3])
    assert landed.annotations["liq_cue_in"] == "15.000"  # resumed: 10 s heard + 5 s away
    assert (await rig.item(sid, 1)).annotations["final"] == "true"
    rig.failed(sid, 1)
    rig.elapse(GRACE_S - 1)
    assert rig.service.frozen_sessions() == []  # D31: nothing started, 30 s from open
    rig.elapse(1)
    assert rig.service.frozen_sessions() == [sid]  # the failed clip did not move it
    rig.started(sid, 0)  # the last song starts after all; the watchdog follows it now
    assert rig.service.open_sessions == 1
    rig.service.close(sid)
    told = await drain(events)
    assert told[-1] == Status(StatusKind.ENDED)
    assert rig.bookmarks.get(KEY) is None

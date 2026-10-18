"""Station liners, PR L2 Task 11: the stream service plays a liner every N songs.

Requirements:
- LR6: a liner is its own item, sent with ``liner_payload``; no title is shown.
- LR7: the count runs per session from tune-in, and the landing is song 1. Songs the walk
  skips don't count; a handed-out song that fails does; a failed liner resets the count. The
  cadence is read at tune-in.
- LR8: a shuffle per session, never the same liner twice in a row across a reshuffle; a
  deleted liner is skipped.
- LR9: timeless. During a liner a reconnect lands, and a bookmark points, at the top of the
  next song; the watchdog follows the liner; nothing else counts liner time.
- LR10: no liner right before the sign-off; the first item is a song. An empty library or a
  blank cadence changes nothing, and a failed liner read plays the song.
- Review Focus 3 (a deleted liner, a failed liner), 4 (the cadence changed mid-session) and
  5 (the watchdog and a playing liner).

The rig (``liner_rig.py``) is a station with cadence 2, liners ``a`` and ``b`` and a shuffle
that sorts descending, so a session's liners come ``b``, ``a``. Its day is songs
``s0``..``s9``, 200 s each, with no cues; a clock tune-in lands 60 s into ``s0``. Where a
stream lands is read from its seq 0: the song sent and ``liq_cue_in`` (no cues, so it is the
offset, D23). In the timeless tests the listener's engine runs a little ahead of the log
(each song is followed 10 s before its end), so the bookmark has not expired, and a clock
tune-in, the live position and the "next song from the top" all land on different points.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from backend.domain.liners import PlayableLiner
from backend.domain.streaming import (
    CueAnalysis,
    CuePoints,
    EndOfScheduleError,
    ItemRef,
    StreamReadError,
)
from backend.playout.liquidsoap_process import long_path
from backend.services.streaming.listener_events import NowPlaying
from backend.services.streaming.payload import FinalClip, ItemPayload, liner_payload
from backend.services.streaming.sessions import Assigned, LinerSlot
from tests.services.streaming.events_rig import drain, make_events_rig
from tests.services.streaming.liner_rig import (
    B_MS,
    LINER_A,
    LINER_B,
    SONG_S,
    LinerRig,
    ascending,
    descending,
    make_liner_rig,
    songs,
)
from tests.services.streaming.schedule import DAY, STATION, song

KEY = "car"
LINER = "liner"
FOLLOWED_S = 190
"""How long each song runs before the engine starts the next item: 10 s before its end."""


@pytest.fixture
def rig(tmp_path: Path) -> LinerRig:
    return make_liner_rig(tmp_path)


def kinds(labels: Sequence[str]) -> list[str]:
    """Songs by label, either liner as ``LINER``."""
    return [LINER if label in ("a", "b") else label for label in labels]


def loud(logs: Sequence[Mapping[str, object]]) -> Counter[tuple[object, object]]:
    """Every log entry above debug, as (event, level)."""
    return Counter(
        (entry.get("event"), entry.get("log_level"))
        for entry in logs
        if entry.get("log_level") != "debug"
    )


async def play_to_the_liner(rig: LinerRig, sid: str) -> None:
    """The engine fetches seqs 0-3 and starts s0 (at its 60 s landing), s1 and the liner at
    seq 2; each song is followed ``FOLLOWED_S`` after it started (s0: 140 s, its rest)."""
    assert kinds(await rig.hand_out(sid, 4)) == ["s0", "s1", LINER, "s2"]
    rig.started(sid, 0)
    rig.elapse(140)
    rig.started(sid, 1)
    rig.elapse(FOLLOWED_S)
    rig.started(sid, 2)


async def landed(rig: LinerRig, sid: str) -> tuple[str, str]:
    """Where ``sid`` landed: its seq 0's song and ``liq_cue_in``."""
    payload = await rig.item(sid, 0)
    return rig.label(payload), payload.annotations["liq_cue_in"]


# ---- the cadence (LR7, LR10) -----------------------------------------------------------------


async def test_liner_after_every_two_songs(rig: LinerRig) -> None:
    """LR7: song, song, liner, and again; the song after a liner is the next one in the log
    (none is skipped for the liner)."""
    sid = await rig.open()

    assert kinds(await rig.hand_out(sid, 6)) == ["s0", "s1", LINER, "s2", "s3", LINER]


async def test_landing_counts_as_song_one(rig: LinerRig) -> None:
    """LR7: the song the listener lands in, mid-song, is song 1."""
    sid = await rig.open()
    landing = await rig.item(sid, 0)

    assert landing.annotations["liq_cue_in"] == "60.000"
    assert kinds([rig.label(landing), *await rig.hand_out(sid, 2, first=1)]) == [
        "s0",
        "s1",
        LINER,
    ]


async def test_cadence_one_alternates_and_starts_with_a_song(tmp_path: Path) -> None:
    """LR7, LR10: with a cadence of 1 a liner follows every song, and the first item after
    tune-in is still a song."""
    rig = make_liner_rig(tmp_path, cadence=1)
    sid = await rig.open()

    assert kinds(await rig.hand_out(sid, 4)) == ["s0", LINER, "s1", LINER]


async def no_liner_baseline(tmp_path: Path, day_of: LinerRig) -> list[ItemPayload]:
    """What a service with no liners at all sends for seqs 0-5 of the same day."""
    plain = make_events_rig(tmp_path / "plain")
    plain.schedule.set_day(STATION, DAY, day_of.day)
    sid = await plain.open()
    return [await plain.item(sid, seq) for seq in range(6)]


async def test_blank_cadence_plays_no_liners(tmp_path: Path) -> None:
    """LR1, LR10: a blank cadence changes nothing: the same items as a service with no liners,
    no liner read, and nothing logged above debug that it does not log."""
    rig = make_liner_rig(tmp_path / "liners", cadence=None)
    with capture_logs() as logs:
        sid = await rig.open()
        sent = [await rig.item(sid, seq) for seq in range(6)]
    with capture_logs() as baseline:
        expected = await no_liner_baseline(tmp_path, rig)

    assert sent == expected
    assert [rig.label(payload) for payload in sent] == ["s0", "s1", "s2", "s3", "s4", "s5"]
    assert rig.liners.reads == 0
    assert loud(logs) == loud(baseline)


async def test_empty_library_plays_no_liners(tmp_path: Path) -> None:
    """LR10 (spec: "no liner, no error, no log noise beyond debug"): cadence 2 with no liners
    sends the same items as a service with no liners, and logs nothing more above debug."""
    rig = make_liner_rig(tmp_path / "liners", with_liners=False)
    with capture_logs() as logs:
        sid = await rig.open()
        sent = [await rig.item(sid, seq) for seq in range(6)]
    with capture_logs() as baseline:
        expected = await no_liner_baseline(tmp_path, rig)

    assert sent == expected
    assert loud(logs) == loud(baseline)


# ---- which liner (LR8) -----------------------------------------------------------------------


async def test_rotation_order_and_no_repeat(rig: LinerRig) -> None:
    """LR8: the session shuffles the library and plays it through (``b``, then ``a``). The
    reshuffle (here one that sorts ascending, so ``a`` first) never starts with ``a``, the
    liner that just played: first and last swap, and ``b`` plays."""
    rig.shuffle.moves = [descending, ascending]
    sid = await rig.open()

    labels = await rig.hand_out(sid, 9)

    assert kinds(labels) == ["s0", "s1", LINER, "s2", "s3", LINER, "s4", "s5", LINER]
    assert [labels[2], labels[5], labels[8]] == ["b", "a", "b"]


async def test_liner_payload_is_sent_for_its_seq(rig: LinerRig) -> None:
    """LR4, LR6, plan note 4: seq 2 is ``liner_payload`` of the liner's slot, sent from
    ``<liners_dir>/<station_id>/<file_name>``, with the liner's own points (autocue's row,
    with its fades removed)."""
    audio = rig.liners.liners[LINER_B].audio_hash
    rig.cues.upsert(
        CueAnalysis(
            audio_hash=audio,
            cues=CuePoints(120, 6_900, 500, 400, 300, -3.5),
            loudness_lufs=-14.0,
            analysis_failed=False,
            analyser_version=1,
        )
    )
    sid = await rig.open()
    await rig.hand_out(sid, 2)

    sent = await rig.item(sid, 2)

    path = rig.liners_dir / str(STATION) / f"{LINER_B}.wav"
    slot = LinerSlot(
        liner=PlayableLiner(LINER_B, f"{LINER_B}.wav", CuePoints(120, 6_900, 0, 0, 300, -3.5)),
        path=path,
        after=Assigned(ItemRef(DAY, 1), rig.day[1], 0),
        after_span_ms=SONG_S * 1000,
    )
    assert sent == liner_payload(2, slot)
    assert sent.path == long_path(path)
    assert rig.liner_file(LINER_B) == path


async def test_same_seq_returns_the_same_liner(rig: LinerRig) -> None:
    """Contract ("the same seq always returns the same item"): asked again, even after the
    liner was deleted, seq 2 is the same liner."""
    sid = await rig.open()
    await rig.hand_out(sid, 2)
    first = await rig.item(sid, 2)
    rig.liners.delete(LINER_B)

    again = await rig.item(sid, 2)

    assert rig.label(first) == "b"
    assert again == first


@pytest.mark.parametrize("with_clip", [True, False], ids=["sign-off clip", "no clip"])
async def test_no_liner_right_before_the_sign_off(tmp_path: Path, with_clip: bool) -> None:
    """LR10 (spec: "if a liner slot falls where the next item is the sign-off, the liner is
    dropped"): the day ends after its 2nd song, so seq 2 is the sign-off clip (or the end),
    never a liner."""
    clip = FinalClip(path=tmp_path / "clip.mp3", span_ms=5_000)
    rig = make_liner_rig(tmp_path, final_clip=clip if with_clip else None, count=2)
    sid = await rig.open()
    assert await rig.hand_out(sid, 2) == ["s0", "s1"]

    if with_clip:
        final = await rig.item(sid, 2)
        assert final.annotations["final"] == "true"
        assert final.path == long_path(clip.path)
    else:
        with pytest.raises(EndOfScheduleError):
            await rig.item(sid, 2)
    with pytest.raises(EndOfScheduleError):
        await rig.item(sid, 3)


async def test_skipped_songs_do_not_count(rig: LinerRig) -> None:
    """LR7: songs the walk skips (an unresolved play, and one too short to play, D9) never
    aired, so two played songs still come before the liner."""
    first, *rest = songs(4)
    gone = song("06:03:10", None, title="Unresolved")
    short = song("06:03:15", 5, title="Too short")
    rig.set_day([first, gone, short, *rest])
    sid = await rig.open()

    # Day positions: 0 s0, 1 unresolved, 2 too short, 3-5 the next three songs.
    assert kinds(await rig.hand_out(sid, 4)) == ["s0", "s3", LINER, "s4"]


# ---- failures (LR7, LR10; Review Focus 3) ----------------------------------------------------


async def test_failed_liner_counts_as_played_and_next_song_follows(rig: LinerRig) -> None:
    """LR7, Review Focus 3 (spec: a liner that fails to load is skipped; the stream goes to
    the next song and the count carries on as if it played): the failure is flagged, seq 3
    is the next song, and the next liner comes two songs later, at seq 5."""
    sid = await rig.open()
    await play_to_the_liner(rig, sid)

    with capture_logs() as logs:
        rig.failed(sid, 2)

    [flagged] = [e for e in logs if e["event"] == "stream_item_failed"]
    assert (flagged["log_level"], flagged["seq"]) == ("warning", 2)
    assert kinds(await rig.hand_out(sid, 3, first=3)) == ["s2", "s3", LINER]


async def test_failed_song_still_counts(rig: LinerRig) -> None:
    """LR7 (spec: a song handed to the engine that then fails still counts): seq 2 is still
    a liner."""
    sid = await rig.open()
    await rig.hand_out(sid, 2)
    rig.started(sid, 0)
    rig.failed(sid, 1)

    assert kinds(await rig.hand_out(sid, 2, first=2)) == [LINER, "s2"]


@pytest.mark.parametrize(
    "deleted", ["before its slot", "held by the rotation", "the whole library"]
)
async def test_liner_deleted_before_its_slot_plays_the_next_one(
    rig: LinerRig, deleted: str
) -> None:
    """LR8, LR10, Review Focus 3: a liner deleted after tune-in is skipped when its turn comes
    and the next one plays; with the whole library deleted the slot plays the song.

    - before its slot: ``b`` (first in the shuffle) is deleted before seq 2, so ``a`` plays.
    - held by the rotation: ``b`` played at seq 2 and ``a`` waits in the order; ``a`` is
      deleted, so the slot at seq 5 plays ``b`` (now the only liner).
    """
    sid = await rig.open()
    if deleted == "held by the rotation":
        assert await rig.hand_out(sid, 3) == ["s0", "s1", "b"]
        rig.liners.delete(LINER_A)
        assert await rig.hand_out(sid, 3, first=3) == ["s2", "s3", "b"]
        return
    assert await rig.hand_out(sid, 2) == ["s0", "s1"]
    rig.liners.delete(LINER_B)
    if deleted == "the whole library":
        rig.liners.delete(LINER_A)
        assert await rig.hand_out(sid, 2, first=2) == ["s2", "s3"]
        return
    assert await rig.hand_out(sid, 2, first=2) == ["a", "s2"]


async def test_liner_read_failure_plays_the_song(rig: LinerRig) -> None:
    """LR10, plan note 9 (D88): a liner read that fails is logged as a warning and the song
    plays; the engine is not told to retry, and the stream plays on."""
    sid = await rig.open()
    await rig.hand_out(sid, 2)
    rig.liners.fail_next_read(StreamReadError("station_liners: canceling statement"))

    with capture_logs() as logs:
        sent = await rig.item(sid, 2)

    assert rig.label(sent) == "s2"
    [warned] = [e for e in logs if e["event"] == "stream_liner_read_failed"]
    assert warned["log_level"] == "warning"
    assert rig.label(await rig.item(sid, 3)) in ("s3", "a", "b")


async def test_cadence_change_applies_to_new_tune_ins_only(rig: LinerRig) -> None:
    """LR7, Review Focus 4: the cadence is read at tune-in. A change while someone listens
    leaves their session at 2; a session opened after it uses 5."""
    tuned_before = await rig.open()
    rig.set_cadence(5)
    tuned_after = await rig.open()

    assert kinds(await rig.hand_out(tuned_before, 6)) == ["s0", "s1", LINER, "s2", "s3", LINER]
    assert kinds(await rig.hand_out(tuned_after, 6)) == ["s0", "s1", "s2", "s3", "s4", LINER]


async def test_two_listeners_count_and_rotate_on_their_own(rig: LinerRig) -> None:
    """LR7, LR8 (per listener session): two sessions, interleaved, each keep their own count
    and their own shuffle. A shared count would put a liner at the second session's seq 1; a
    shared rotation would give the first session a."""
    first = await rig.open()
    assert await rig.hand_out(first, 2) == ["s0", "s1"]
    second = await rig.open()

    assert await rig.hand_out(second, 3) == ["s0", "s1", "b"]
    assert await rig.hand_out(first, 1, first=2) == ["b"]


# ---- on air, timeless (LR6, LR9; Review Focus 5) ---------------------------------------------


async def test_liner_shows_no_title(rig: LinerRig) -> None:
    """LR6 (spec: "no title is shown"; a liner never shows as now playing): once the liner
    starts, ICY still shows the song before it and the channel is told no new title."""
    events = await rig.subscribe(KEY)
    sid = await rig.open(KEY)
    await play_to_the_liner(rig, sid)
    rig.elapse(5)

    titles = [e for e in await drain(events) if isinstance(e, NowPlaying)]

    assert titles == [
        NowPlaying(artist="Artist 0", title="Song 0"),
        NowPlaying(artist="Artist 1", title="Song 1"),
    ]
    assert rig.service.now_playing(sid) == "Artist 1 - Song 1"


async def test_watchdog_follows_a_playing_liner(rig: LinerRig) -> None:
    """LR9, Review Focus 5: while the liner (b: 7 s, played whole) plays, the session is not
    frozen until its span plus the grace (30 s) has passed on the steady clock."""
    sid = await rig.open()
    await play_to_the_liner(rig, sid)

    rig.elapse(B_MS / 1000 + 30 - 0.5)
    assert rig.service.frozen_sessions() == []

    rig.elapse(1)
    assert rig.service.frozen_sessions() == [sid]


async def test_reconnect_during_a_liner_lands_on_the_next_song(rig: LinerRig) -> None:
    """LR9 (D109): 3 s into the liner a same-key reconnect lands at the top of the song after
    it. A clock tune-in would land 193 s into s1; counting liner time, 3 s into s2."""
    old = await rig.open(KEY)
    await play_to_the_liner(rig, old)
    rig.elapse(3)

    reconnect = await rig.open(KEY)

    assert await landed(rig, reconnect) == ("s2", "0.000")


async def test_bookmark_during_a_liner_points_at_the_next_song(rig: LinerRig) -> None:
    """LR9 (spec: "leaving during a liner: the bookmark points at the start of the song after
    the liner"): left 3 s into the liner, a resume at once lands at the top of s2. A clock
    tune-in would land 193 s into s1."""
    old = await rig.open(KEY)
    await play_to_the_liner(rig, old)
    rig.elapse(3)
    rig.service.close(old)

    resumed = await rig.open(KEY)

    assert await landed(rig, resumed) == ("s2", "0.000")


async def test_liner_time_is_not_counted_on_reconnect(rig: LinerRig) -> None:
    """LR9: the liner (7 s) played, then s2 started; 10 s into s2 a reconnect lands 10 s into
    it. A clock tune-in would land 7 s in; counting liner time, 17 s."""
    old = await rig.open(KEY)
    await play_to_the_liner(rig, old)
    rig.elapse(B_MS / 1000)
    rig.started(old, 3)
    rig.elapse(10)

    reconnect = await rig.open(KEY)

    assert await landed(rig, reconnect) == ("s2", "10.000")


@pytest.mark.parametrize("leave", ["reconnect", "bookmark"])
async def test_liner_after_the_landing_resumes_at_the_next_songs_top(
    tmp_path: Path, leave: str
) -> None:
    """LR9, plan note 12 (``after_span_ms`` is the song's sent span, from the landing's
    offset): with cadence 1 the first liner follows the landing, 60 s into s0. 3 s into the
    liner, a reconnect or a resume lands at the top of s1. s0's whole span would give 60 s
    into s1; a clock tune-in, 193 s into s0."""
    rig = make_liner_rig(tmp_path, cadence=1)
    old = await rig.open(KEY)
    assert kinds(await rig.hand_out(old, 3)) == ["s0", LINER, "s1"]
    rig.started(old, 0)
    rig.elapse(130)  # the liner starts 10 s before s0's logged end (listener ahead of the log)
    rig.started(old, 1)
    rig.elapse(3)
    if leave == "bookmark":
        rig.service.close(old)

    again = await rig.open(KEY)

    assert await landed(rig, again) == ("s1", "0.000")


async def test_reconnect_starts_the_count_and_the_shuffle_over(rig: LinerRig) -> None:
    """Spec "Timeless" (reconnect): the liner count and the shuffle start over. The old session
    played b, holds a next, and has one song (s2) since its liner. The reconnect lands in s2 as
    its song 1, plays s3, then a fresh shuffle's first liner (b). A carried count would put a
    liner at seq 1; a carried rotation (or a carried "last") would play a."""
    old = await rig.open(KEY)
    await play_to_the_liner(rig, old)
    rig.elapse(B_MS / 1000)
    rig.started(old, 3)
    rig.elapse(10)

    reconnect = await rig.open(KEY)

    assert await rig.hand_out(reconnect, 3) == ["s2", "s3", "b"]

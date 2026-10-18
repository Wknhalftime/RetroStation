"""Station liners, follow-up to PR L2 (#171) final review M1: the liner read is bounded like
D85's cue re-read, so a slow database never holds a song slot near the engine's timeout.

Requirements:
- Plan note 9 / D88: "a liner never holds up the music". A liner read that fails, or takes
  too long, logs ``stream_liner_read_failed`` and the song plays.
- D86(c), applied to the liner read: it runs on the re-read's own short-lived connections
  (``StreamPorts.cue_reread_repos``: short statement and lock limits) and is bounded by
  ``CueRereadLimits``: at most ``timeout_s`` of waiting and ``in_flight`` reads at once.
  A read past the limit is left running on its thread; it is not waited for.

The rig is the liner rig (cadence 2, liners ``a`` and ``b``, the shuffle plays ``b`` first)
rewired by ``liner_reads_rig.rewired``: its general connection reads no liners, so a liner can
only come from the short one.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

from structlog.testing import capture_logs

from backend.services.streaming.cue_reread import CueRereadLimits
from backend.services.streaming.service import StreamRepos
from tests.services.streaming.liner_reads_rig import HeldLiners, held_liners, rewired
from tests.services.streaming.liner_rig import LinerRig, make_liner_rig

FAST = CueRereadLimits(timeout_s=0.05)


def bounded_rig(tmp_path: Path, limits: CueRereadLimits) -> tuple[LinerRig, HeldLiners]:
    """The liner rig whose liners are read only through the short connections, held by
    ``HeldLiners``, under ``limits``."""
    rig = make_liner_rig(tmp_path)
    liners = held_liners(rig)
    general = StreamRepos(stations=rig.stations, settings=rig.settings, schedule=rig.schedule)
    short = StreamRepos(
        stations=rig.stations, settings=rig.settings, schedule=rig.schedule, liners=liners
    )
    return rewired(rig, tmp_path, general=general, short=short, limits=limits), liners


def failures(logs: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    return [e for e in logs if e["event"] == "stream_liner_read_failed"]


async def test_the_liner_read_uses_the_rereads_connections(tmp_path: Path) -> None:
    """D86(c): the liners come from the short-lived connections (``cue_reread_repos``), one
    read for the due slot; the general connection, which reads none here, is not used."""
    rig, liners = bounded_rig(tmp_path, CueRereadLimits())
    sid = await rig.open()

    assert await rig.hand_out(sid, 3) == ["s0", "s1", "b"]
    assert liners.entered == 1


async def test_a_slow_liner_read_never_holds_the_slot(tmp_path: Path) -> None:
    """Plan note 9 with D86(c): past ``timeout_s`` the due slot plays its song while the read
    is provably still running (``left`` is not set: a version with no limit, or one reading
    on the event loop, returns only after the read ended). The timeout is logged as a failed
    liner read, at warning."""
    rig, liners = bounded_rig(tmp_path, FAST)
    sid = await rig.open()
    await rig.hand_out(sid, 2)
    hold = threading.Event()
    liners.hold = hold
    try:
        with capture_logs() as logs:
            sent = await rig.item(sid, 2)
        assert not liners.left.is_set()  # the read is still inside the fake
        assert liners.inside.wait(5)
        assert rig.label(sent) == "s2"
        [warned] = failures(logs)
        assert (warned["log_level"], warned["seq"], warned["error_type"]) == (
            "warning",
            2,
            "timeout",
        )
    finally:
        hold.set()


async def test_liner_reads_are_capped_in_flight(tmp_path: Path) -> None:
    """D86(c): with ``in_flight`` reads already running, a due slot starts no other read and
    plays its song; the skipped read is logged as a failed liner read ("busy"). After the
    timed-out read at seq 2, seq 3 is due again (a failed read reads as none, LR10)."""
    rig, liners = bounded_rig(tmp_path, CueRereadLimits(timeout_s=0.05, in_flight=1))
    sid = await rig.open()
    await rig.hand_out(sid, 2)
    hold = threading.Event()
    liners.hold = hold
    try:
        with capture_logs() as logs:
            assert rig.label(await rig.item(sid, 2)) == "s2"
            assert rig.label(await rig.item(sid, 3)) == "s3"
        assert liners.entered == 1
        assert [(e["seq"], e["error_type"]) for e in failures(logs)] == [
            (2, "timeout"),
            (3, "busy"),
        ]
    finally:
        hold.set()

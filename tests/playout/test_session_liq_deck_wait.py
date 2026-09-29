"""Locked test: with every deck busy, the push waits for the first to free (spec: Engine
"If every deck is busy, the push waits (DECK_WAIT) for the first one to free"; "PR D must add
a locked test for the all-decks-busy wait"). It passes today; it locks the behaviour.

Margins are at least 5 s so that timing noise cannot change the outcome. Fade shapes are not
asserted (the requirement is the wait, not the fade). Start times are the engine's own
STARTED lines, not when the stub received the POST, so HTTP latency cannot move them."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from stream_stub import first_audio, record_for

from tests.playout.test_session_liq_decks import (
    Cue,
    band_levels,
    cued_session,
    job,  # noqa: F401 - the fixture, imported so pytest finds it
    overlap_violations,
    schedule,
)
from tests.playout.test_session_liq_errors import (
    FIRST_AUDIO_TIMEOUT_S,
    SessionSetup,
    engine_lines,
    rs_trace,
)

pytestmark = [pytest.mark.slow, pytest.mark.timeout(180)]

LONG = Cue(span_s=30.0, sn_rem_s=16.0, fade_in_s=3.0, fade_out_s=8.0)
FLASH = Cue(span_s=8.0, sn_rem_s=7.0, fade_in_s=1.0, fade_out_s=1.0)  # D9: 1 + 1 + 3 <= 8
# Starts the next push 1 s in, like FLASH, but keeps its deck busy for 16 s.
HOLD = Cue(span_s=16.0, sn_rem_s=15.0, fade_in_s=1.0, fade_out_s=1.0)
SONG = Cue(span_s=12.0, sn_rem_s=4.0, fade_in_s=3.0, fade_out_s=4.0)
_STARTED = re.compile(r" seq=(\d+) t=([0-9.]+)")


def engine_start_times(engine_log: Path) -> dict[int, float]:
    """Each seq's first ``RS STARTED`` time, on the engine's clock."""
    times: dict[int, float] = {}
    for line in engine_lines(engine_log, "STARTED"):
        found = _STARTED.search(line)
        if found:
            times.setdefault(int(found.group(1)), float(found.group(2)))
    return times


def test_with_every_deck_busy_the_next_item_waits_then_each_plays_once(
    tmp_path: Path,
    liquidsoap_exe: Path,
    liq_cache: Path,
    job: object,  # noqa: F811
) -> None:
    # Pushes: item 1 at ~14 s (A plays to ~30 s), item 2 at ~15 s, item 3 at ~16 s, when B
    # runs to ~22 s and C to ~31 s: every deck is busy for ~6 s, so item 3 waits for B, the
    # first to free. Waiting for any other deck would start it at ~30 s or later.
    cues = {0: LONG, 1: FLASH, 2: HOLD, 3: SONG, 4: SONG}
    setup = SessionSetup(liquidsoap_exe, liq_cache, tmp_path, None)
    recording = tmp_path / "out.mp3"
    with cued_session(job, schedule(tmp_path, cues), setup, cues) as session:
        _, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
        record_for(response, 120, recording)
        response.close()
        failed, started = session.stub.failed.seqs(), session.stub.started.seqs()
        started_at = engine_start_times(session.engine_log)
        waits = engine_lines(session.engine_log, "DECK_WAIT")
        trace = rs_trace(session.engine_log)
    assert waits, f"harness: no push ever waited for a deck\n{trace}"
    assert failed == [], f"healthy items reported failed: {failed}\n{trace}"
    assert started == sorted(cues), f"started {started}\n{trace}"
    first_free = started_at[1] + FLASH.span_s
    assert started_at[3] >= first_free - 0.5, f"item 3 did not wait\n{trace}"
    assert started_at[3] <= first_free + 3.0, f"item 3 waited past the first free deck\n{trace}"
    levels = band_levels(recording, sorted(cues))
    assert not overlap_violations(levels, cues), trace

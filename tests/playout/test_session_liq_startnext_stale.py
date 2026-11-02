"""session.liq: a short item after a long segue on the same deck still starts the next item.

on_position reads its position when a deck's next track begins, before on_track has set the
new track's ``sn_rem``. If the deck's previous track had a ``sn_rem`` at least as long as
the new track's span, the new track's start-next callback was spent at once and never fired:
the stream dropped to filler (KKDM 2000, 2026-11-01, a 5.6 s liner after a song with a 6 s
segue). Decks rotate A -> B -> C, so item 4 lands on item 1's deck.
"""

from __future__ import annotations

from pathlib import Path

import pytest

windows_job = pytest.importorskip("backend.playout.windows_job", reason="Windows only")

from stream_stub import first_audio, record_for  # noqa: E402

from tests.playout.test_session_liq_decks import (  # noqa: E402
    Cue,
    cued_session,
    job,  # noqa: F401 - pytest fixture
    schedule,
)
from tests.playout.test_session_liq_errors import (  # noqa: E402
    FIRST_AUDIO_TIMEOUT_S,
    SessionSetup,
    rs_trace,
)

pytestmark = [pytest.mark.slow, pytest.mark.timeout(180)]

SONG = Cue(span_s=12.0, sn_rem_s=4.0, fade_in_s=1.0, fade_out_s=1.0)
LONG_SEGUE = Cue(span_s=16.0, sn_rem_s=6.0, fade_in_s=1.0, fade_out_s=1.0)
# Shorter than the segue before it on the same deck (5.5 < 6.0).
LINER = Cue(span_s=5.5, sn_rem_s=3.0, fade_in_s=0.3, fade_out_s=0.5)


def test_short_item_after_a_long_segue_on_its_deck_starts_the_next_item(
    tmp_path: Path,
    liquidsoap_exe: Path,
    liq_cache: Path,
    job: object,  # noqa: F811
) -> None:
    cues = {0: SONG, 1: LONG_SEGUE, 2: SONG, 3: SONG, 4: LINER, 5: SONG}
    setup = SessionSetup(liquidsoap_exe, liq_cache, tmp_path, None)
    with cued_session(job, schedule(tmp_path, cues), setup, cues) as session:
        _, response = first_audio(session.port, session.started_at, FIRST_AUDIO_TIMEOUT_S)
        # The schedule ends after the last item and the stream closes (end of schedule).
        record_for(response, 120, tmp_path / "out.mp3")
        response.close()
        failed, started = session.stub.failed.seqs(), session.stub.started.seqs()
        trace = rs_trace(session.engine_log)
    assert failed == [], f"healthy items reported failed: {failed}\n{trace}"
    assert started == sorted(cues), f"started {started}\n{trace}"

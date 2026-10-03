"""A bad stored sign-off plays no clip, even with a default configured (G1 review, P1).

Requirements: design note 6 ("a bad stored value plays no clip and logs
``stream_setting_invalid``"); PG4 (the default plays only when the user has no clip). The
locked T4.10 covers a bad value with no default; this covers it with one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

from backend.domain.streaming import EndOfScheduleError
from backend.services.streaming.payload import FinalClip
from tests.services.streaming.sign_off_rig import make_sign_off_rig, morning

CLIP_SEQ = 3


async def test_a_bad_stored_sign_off_plays_no_clip_even_with_a_default(tmp_path: Path) -> None:
    default = FinalClip(path=Path("assets/default-signoff.flac"), span_ms=4_000)
    rig = make_sign_off_rig(tmp_path, default_clip=default)
    morning(rig)
    rig.store_raw('{"file_name": "../../escape.mp3", "format": "mp3"}')
    sid = await rig.open()
    with capture_logs() as logs:
        assert await rig.play_to_end(sid) == CLIP_SEQ
    invalid = [e for e in logs if e["event"] == "stream_setting_invalid"]
    assert invalid and invalid[0]["setting"] == "stream_sign_off"
    with pytest.raises(EndOfScheduleError):
        await rig.item(sid, CLIP_SEQ)

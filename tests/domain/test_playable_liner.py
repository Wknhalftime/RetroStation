"""Station liners, PR L2 Task 9 (carried from the L1 review, Task 1 minor 3): a
``PlayableLiner``'s file name is where the stream builds the liner's path, so it must be a
stored liner's name (``LINER_FILE_NAME``: its id, then its format) and nothing that could
leave the station's folder (LR3, LR4).
"""

from __future__ import annotations

from uuid import UUID

import pytest

from backend.domain.liners import InvalidLinerValueError, PlayableLiner
from backend.domain.streaming import CuePoints

LINER_ID = UUID("6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab")
POINTS = CuePoints(0, 7_000, 0, 0, 0, 0.0)


def test_playable_liner_refuses_a_bad_file_name() -> None:
    for bad in ("../x.flac", "a.flac"):
        with pytest.raises(InvalidLinerValueError):
            PlayableLiner(LINER_ID, bad, POINTS)
    for fmt in ("flac", "mp3", "wav"):  # a stored liner's own name is fine
        assert PlayableLiner(LINER_ID, f"{LINER_ID}.{fmt}", POINTS).file_name == (
            f"{LINER_ID}.{fmt}"
        )

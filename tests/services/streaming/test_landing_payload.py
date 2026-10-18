"""What the service sends for an item it hands out (spec: D80, a landing whose tail is shorter
than D9's minimum "plays with D23's values"; D23, "sn_rem = 0 and liq_amplify = 0.0 dB, sent
explicitly. cue_out = duration_ms"; D112, no fade-in, so D9's minimum is fade_out + 3 s).
The locked test_payload pins item_payload itself."""

from __future__ import annotations

import pytest

from backend.domain.streaming import CuePoints
from backend.services.streaming.payload import item_payload, landing_payload
from tests.services.streaming.test_payload import CUES, TIMING, item_with


def test_a_landing_short_of_the_minimum_is_sent_with_d23_values() -> None:
    """D80: CUES leave D9's minimum (D112: the 5 s fade-out + 3 s) up to 171.5 s; at
    171.501 s the song plays with D23's values (to its end, the default fade-out and no
    fade-in, no overlap, no gain change). Coordinator ruling (Revision 2): it starts at the
    same moment of the song, so the offset, measured from the 1.5 s cue-in, is carried onto
    the file's timeline: 1.5 + 171.501 = 173.001 s."""
    assert landing_payload(0, item_with(CUES), 171_501, TIMING).annotations == {
        "item_seq": "0",
        "liq_cue_in": "173.001",
        "liq_cue_out": "200.000",
        "liq_fade_in": "0.000",
        "liq_fade_out": "4.000",
        "sn_rem": "0.000",
        "liq_amplify": "0.0 dB",
        "title": "Fernando",
        "artist": "ABBA",
    }


@pytest.mark.parametrize(
    ("cues", "offset_ms"),
    [(CUES, 0), (CUES, 171_500), (None, 0), (None, 193_000)],
)
def test_a_landing_that_fits_is_sent_as_item_payload_sends_it(
    cues: CuePoints | None, offset_ms: int
) -> None:
    """D80 changes nothing else: a tail that fits (D9) is sent exactly as before."""
    item = item_with(cues)
    assert landing_payload(4, item, offset_ms, TIMING) == item_payload(4, item, offset_ms, TIMING)

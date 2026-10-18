"""Station liners, PR L2 Task 10: the liner's place in a session (``LinerSlot``,
``songs_since_liner``) and what the stream sends Liquidsoap for it (``liner_payload``).

Requirements: LR6 (the liner starts at the outgoing song's EOM, plays at full level with no
fade-in or fade-out, and the next song starts at the liner's EOM: ``sn_rem`` is its own
start-next; no title is shown), LR7 (the count runs per session from tune-in, the landing is
song 1, and a liner restarts it). Plan Task 10: the payload is ``item_seq``, ``liq_cue_in``
and ``liq_cue_out`` from the cues, both fades ``"0.000"``, ``sn_rem`` from ``start_next_ms``,
``liq_amplify`` as ``"{gain:.1f} dB"``, an empty title and artist, no ``final`` key, and the
path through ``long_path``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from uuid import UUID

from backend.domain.liners import PlayableLiner
from backend.domain.streaming import CuePoints, ItemRef, ScheduleItem
from backend.playout.liquidsoap_process import long_path
from backend.services.streaming.payload import ItemPayload, liner_payload
from backend.services.streaming.sessions import (
    Assigned,
    LinerSlot,
    StreamSession,
    songs_since_liner,
)
from tests.services.streaming.schedule import DAY, song

LINER_ID = UUID("6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab")
STATION_ID = UUID("0a1b2c3d-4e5f-4a6b-9c7d-8e9fa0b1c2d3")
FOLDER = Path("D:/stream/liners") / str(STATION_ID)
SONG = song("06:00:00", title="Fernando", artist="ABBA")


def slot(cues: CuePoints, *, after: Assigned | None = None) -> LinerSlot:
    after = after if after is not None else Assigned(ItemRef(DAY, 0), SONG, 0)
    return LinerSlot(
        liner=PlayableLiner(LINER_ID, f"{LINER_ID}.flac", cues),
        path=FOLDER / f"{LINER_ID}.flac",
        after=after,
        after_span_ms=200_000,
    )


def test_liner_payload_annotations() -> None:
    """LR6: the cues' positions, no fades, the next item at the liner's EOM, autocue's gain,
    no title and no ``final`` mark; the path as the engine opens it."""
    liner = slot(CuePoints(120, 6_900, 0, 0, 300, -3.5))

    sent = liner_payload(4, liner)

    assert sent == ItemPayload(
        path=long_path(FOLDER / f"{LINER_ID}.flac"),
        annotations={
            "item_seq": "4",
            "liq_cue_in": "0.120",
            "liq_cue_out": "6.900",
            "liq_fade_in": "0.000",
            "liq_fade_out": "0.000",
            "sn_rem": "0.300",
            "liq_amplify": "-3.5 dB",
            "title": "",
            "artist": "",
        },
    )
    assert "final" not in sent.annotations
    assert liner.span_ms == 6_780


def test_liner_payload_whole_file() -> None:
    """LR4, LR6: a liner before analysis plays whole, with the next item at its very end,
    at its recorded level."""
    sent = liner_payload(2, slot(CuePoints(0, 7_000, 0, 0, 0, 0.0)))

    assert dict(sent.annotations) == {
        "item_seq": "2",
        "liq_cue_in": "0.000",
        "liq_cue_out": "7.000",
        "liq_fade_in": "0.000",
        "liq_fade_out": "0.000",
        "sn_rem": "0.000",
        "liq_amplify": "0.0 dB",
        "title": "",
        "artist": "",
    }


def test_liner_payload_never_fades() -> None:
    """LR4, LR6 (spec: "No fade-in, no fade-out, ever"): whatever fades the points carry, the
    liner is sent with both fades at zero."""
    sent = liner_payload(3, slot(CuePoints(120, 6_900, 500, 400, 300, -3.5)))

    assert (sent.annotations["liq_fade_in"], sent.annotations["liq_fade_out"]) == (
        "0.000",
        "0.000",
    )


def no_day(day: date) -> Sequence[ScheduleItem]:
    return []


def session_with(handed_out: list[str]) -> StreamSession:
    """A session whose seqs were handed out in order: ``"song"`` or ``"liner"``."""
    session = StreamSession(load_day=no_day, opened_at=datetime(2000, 1, 1), bookmark_key=None)
    previous = Assigned(ItemRef(DAY, 0), SONG, 60_000)
    for seq, kind in enumerate(handed_out):
        if kind == "song":
            previous = Assigned(ItemRef(DAY, seq), song("06:00:00"), 0 if seq else 60_000)
            session.assigned[seq] = previous
        else:
            session.assigned[seq] = slot(CuePoints(0, 7_000, 0, 0, 0, 0.0), after=previous)
    return session


def test_songs_since_liner_counts_from_tune_in() -> None:
    """LR7: the count starts at tune-in, and the landing (seq 0) is song 1."""
    session = session_with(["song", "song"])

    assert songs_since_liner(session, 1) == 1
    assert songs_since_liner(session, 2) == 2


def test_songs_since_liner_restarts_after_a_liner() -> None:
    """LR7: a liner restarts the count; only songs handed out after it count."""
    session = session_with(["song", "song", "liner", "song"])

    assert songs_since_liner(session, 3) == 0
    assert songs_since_liner(session, 4) == 1

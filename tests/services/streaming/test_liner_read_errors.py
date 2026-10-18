"""Station liners, PR L2 final review: the liner read never holds up the music, and the
rotation moves on only when the slot is stored (``ARCHITECTURE.md``, "Liners on air").

- M2: a domain value error from the liner read (``InvalidLinerValueError``,
  ``InvalidStreamValueError``) is a warning and the song plays, as a failed read is (D88).
- The shuffle's rotation moves on once the liner's slot is stored, never for a slot a
  concurrent request stored first.

Uses the liner rig by import.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

from backend.domain.liners import InvalidLinerValueError, LinerRotation
from backend.domain.streaming import InvalidStreamValueError
from backend.services.streaming.payload import ItemPayload
from backend.services.streaming.service import ItemCall
from backend.services.streaming.sessions import Assigned, LinerSlot, StreamSession
from tests.services.streaming.liner_rig import LinerRig, make_liner_rig


@pytest.fixture
def rig(tmp_path: Path) -> LinerRig:
    return make_liner_rig(tmp_path)


@pytest.mark.parametrize(
    "error",
    [
        InvalidLinerValueError("station_liners: bad row"),
        InvalidStreamValueError("stream_cues: bad row"),
    ],
    ids=["liner value", "stream value"],
)
async def test_value_error_from_the_liner_read_plays_the_song(
    rig: LinerRig, error: Exception
) -> None:
    sid = await rig.open()
    await rig.hand_out(sid, 2)
    rig.liners.fail_next_read(error)

    with capture_logs() as logs:
        sent = await rig.item(sid, 2)

    assert rig.label(sent) == "s2"
    [warned] = [e for e in logs if e["event"] == "stream_liner_read_failed"]
    assert warned["log_level"] == "warning"
    assert warned["error_type"] == type(error).__name__
    assert warned["error"] == str(error)
    # the stream plays on: the next slot reads again and gets a liner
    assert rig.label(await rig.item(sid, 3)) in ("a", "b")


async def test_rotation_moves_once_the_slot_is_stored(rig: LinerRig) -> None:
    sid = await rig.open()
    await rig.hand_out(sid, 2)
    session = rig.service._sessions[sid]
    assert session.liner_rotation == LinerRotation()

    assert rig.label(await rig.item(sid, 2)) == "b"

    assert isinstance(session.assigned[2], LinerSlot)
    assert session.liner_rotation.last is not None


async def test_rotation_does_not_move_for_a_slot_a_concurrent_request_stored(
    rig: LinerRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another request stores seq 2 between the pick and the store (a stand-in for the race
    ``_assign`` guards against): its slot wins and the rotation stays where it was."""
    sid = await rig.open()
    await rig.hand_out(sid, 2)
    session = rig.service._sessions[sid]
    winner = session.assigned[1]
    assert isinstance(winner, Assigned)
    real_assign = rig.service._assign

    def assign_after_a_rival(
        call: ItemCall, stored: StreamSession, assigned: Assigned | LinerSlot
    ) -> ItemPayload:
        if isinstance(assigned, LinerSlot):
            stored.assigned[call.seq] = winner
        return real_assign(call, stored, assigned)

    monkeypatch.setattr(rig.service, "_assign", assign_after_a_rival)

    await rig.item(sid, 2)

    assert session.assigned[2] is winner
    assert session.liner_rotation == LinerRotation()

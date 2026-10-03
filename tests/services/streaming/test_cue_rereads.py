"""Re-reading a song's stored cues without ever holding or failing the item (spec: D85, "If
that read fails, the song uses what was read at tune-in; playback never waits on or fails
because of it"; the D85 brief: a timeout so a slow read never delays placement; E1's lesson:
a lasting failure must not flood System Logs)."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from backend.domain.streaming import CuePoints
from backend.services.streaming.cue_reread import CueRereadLimits, CueRereads, Unread
from backend.services.streaming.service import StreamServiceConfig
from tests.services.streaming.helpers import BACKEND

STORED = CuePoints(1_500, 181_000, 2_000, 5_000, 4_500, -6.2)
FAST = CueRereadLimits(timeout_s=0.05, in_flight=1)


@dataclass
class Store:
    """A fake stored-cues read: answers ``rows``, raises the next of ``failures`` when given,
    waits on ``hold`` when set, and records the thread each read ran on. ``left`` is set when
    a read ends, however it ends (review I1)."""

    rows: dict[UUID, CuePoints] = field(default_factory=dict)
    failures: list[OSError | None] = field(default_factory=list)
    hold: threading.Event | None = None
    entered: threading.Event = field(default_factory=threading.Event)
    left: threading.Event = field(default_factory=threading.Event)
    threads: list[int] = field(default_factory=list)

    def __call__(self, file_id: UUID) -> CuePoints | None:
        self.threads.append(threading.get_ident())
        self.entered.set()
        try:
            if self.hold is not None:
                assert self.hold.wait(timeout=10), "the test never released the read"
            if self.failures:
                failure = self.failures.pop(0)
                if failure is not None:
                    raise failure
            return self.rows.get(file_id)
        finally:
            self.left.set()


async def test_the_stored_answer_comes_from_a_worker_thread() -> None:
    """D85: a stored row, or None for no row, read off the event loop."""
    cued, uncued = uuid4(), uuid4()
    store = Store(rows={cued: STORED})
    rereads = CueRereads(store, CueRereadLimits())
    assert [await rereads.stored_cues(cued), await rereads.stored_cues(uncued)] == [STORED, None]
    assert threading.get_ident() not in store.threads


async def test_a_read_past_the_time_limit_is_unread_while_it_still_runs() -> None:
    """D85: playback never waits on the read; the answer is "unread" (tune-in values), given
    while the read is provably still running."""
    hold = threading.Event()
    store = Store(hold=hold)
    rereads = CueRereads(store, FAST)
    try:
        answer = await rereads.stored_cues(uuid4())
        assert not store.left.is_set()  # the answer came back while the read was running
        assert answer == Unread("timeout")
        assert store.entered.wait(5)  # it was read
    finally:
        hold.set()


async def test_a_failing_read_is_unread_and_logged_once_until_a_success() -> None:
    """D85: a failed read never fails the item; one warning, then debug, until a read works."""
    file_id = uuid4()
    store = Store(
        rows={file_id: STORED},
        failures=[OSError("connection refused"), OSError("connection refused"), None, OSError()],
    )
    rereads = CueRereads(store, CueRereadLimits())
    with capture_logs() as logs:
        answers = [await rereads.stored_cues(file_id) for _ in range(4)]
    assert [isinstance(a, Unread) for a in answers] == [True, True, False, True]
    assert answers[2] == STORED
    levels = [e["log_level"] for e in logs if e["event"] == "stream_cue_reread_failed"]
    assert levels == ["warning", "debug", "warning"]


async def test_no_more_reads_run_than_the_limit() -> None:
    """A read that timed out still holds its thread; past the limit a song is not read at all
    (its tune-in values play), so slow reads cannot pile up threads. Review M1: being busy is
    load shedding, a debug line, not a failure warning."""
    hold = threading.Event()
    store = Store(hold=hold)
    rereads = CueRereads(store, FAST)
    try:
        with capture_logs() as logs:
            answers = [await rereads.stored_cues(uuid4()) for _ in range(2)]
        assert answers == [Unread("timeout"), Unread("busy")]
        assert store.entered.wait(5)
        assert len(store.threads) == 1
        failed = [e["log_level"] for e in logs if e["event"] == "stream_cue_reread_failed"]
        skipped = [e["log_level"] for e in logs if e["event"] == "stream_cue_reread_skipped"]
        assert (failed, skipped) == (["warning"], ["debug"])
    finally:
        hold.set()


@pytest.mark.parametrize(
    ("limits", "field_name"),
    [
        ({"timeout_s": 0.0}, "timeout_s"),
        ({"timeout_s": math.nan}, "timeout_s"),
        ({"in_flight": 0}, "in_flight"),
    ],
)
def test_the_limits_are_validated(limits: dict[str, float], field_name: str) -> None:
    with pytest.raises(ValueError, match=f"CueRereadLimits.{field_name}"):
        CueRereadLimits(**limits)  # type: ignore[arg-type]


def test_the_service_rereads_for_half_a_second_two_at_once(tmp_path: Path) -> None:
    """D86(c): "a re-read is bounded at 0.5 s with at most 2 at once"."""
    config = StreamServiceConfig(callback_base_url=BACKEND, log_dir=tmp_path)
    assert config.cue_reread == CueRereadLimits(timeout_s=0.5, in_flight=2)

"""D85: a song's stored cues, re-read just before it is handed to the engine, without ever holding
or failing the item.

Spec: D85 ("If that read fails, the song uses what was read at tune-in; playback never waits on
or fails because of it"); D86(c) (a re-read is bounded at 0.5 s with at most 2 at once). The read
is synchronous, so it runs in a worker thread; ``asyncio.wait`` bounds the wait without
cancelling the thread's task or raising (review I2), so this module catches nothing.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import structlog

from backend.domain.streaming import CuePoints, InvalidStreamValueError

logger = structlog.get_logger()

type ReadStoredCues = Callable[[UUID], CuePoints | None]


@dataclass(frozen=True)
class CueRereadLimits:
    """How long a re-read may take and how many may run at once (D86(c))."""

    timeout_s: float = 0.5
    in_flight: int = 2

    def __post_init__(self) -> None:
        if not (math.isfinite(self.timeout_s) and self.timeout_s > 0):
            raise InvalidStreamValueError(
                f"CueRereadLimits.timeout_s must be finite and > 0, got {self.timeout_s}"
            )
        if self.in_flight < 1:
            raise InvalidStreamValueError(
                f"CueRereadLimits.in_flight must be >= 1, got {self.in_flight}"
            )


@dataclass(frozen=True)
class Unread:
    """The stored cues could not be read in time or at all (D85): the song keeps its tune-in
    values."""

    reason: str


class CueRereads:
    """Reads a file's stored cues in a worker thread, bounded in time and in threads, so the read
    never delays or fails an item (D85). Event loop only."""

    def __init__(self, read: ReadStoredCues, limits: CueRereadLimits) -> None:
        self._read = read
        self._limits = limits
        self._running = 0
        self._failing = False

    async def stored_cues(self, file_id: UUID) -> CuePoints | None | Unread:
        """The file's stored cues, None when it has none, or ``Unread`` when the read is busy,
        late or failed. Never raises; a late read keeps running on its own thread."""
        if self._running >= self._limits.in_flight:
            logger.debug("stream_cue_reread_skipped", file_id=str(file_id))
            return Unread("busy")
        task = self._start(file_id)
        done, _ = await asyncio.wait({task}, timeout=self._limits.timeout_s)
        answer = _answer(task, done)
        self._note(file_id, answer)
        return answer

    def _start(self, file_id: UUID) -> asyncio.Task[CuePoints | None]:
        """Count one read in flight and start it in a worker thread."""
        self._running += 1
        task = asyncio.ensure_future(asyncio.to_thread(self._read, file_id))
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[CuePoints | None]) -> None:
        """Lower the in-flight count and retrieve the read's exception, so a late failure is
        never reported as "never retrieved"."""
        self._running -= 1
        if not task.cancelled():
            task.exception()

    def _note(self, file_id: UUID, answer: CuePoints | None | Unread) -> None:
        """Warn on the first failure after a success (or before any read ran), then debug, so a
        lasting failure does not flood System Logs; a success clears the flag."""
        if not isinstance(answer, Unread):
            self._failing = False
            return
        log = logger.debug if self._failing else logger.warning
        log("stream_cue_reread_failed", file_id=str(file_id), reason=answer.reason)
        self._failing = True


def _answer(
    task: asyncio.Task[CuePoints | None], done: set[asyncio.Task[CuePoints | None]]
) -> CuePoints | None | Unread:
    """What a read that was waited on answers: its result, or why it is unread."""
    if task not in done:
        return Unread("timeout")
    if task.cancelled():  # only a shutdown cancels the read's task
        return Unread("cancelled")
    if (error := task.exception()) is not None:
        return Unread(repr(error))
    return task.result()

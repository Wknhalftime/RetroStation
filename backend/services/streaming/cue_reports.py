"""No-cue reports from the player to the cue owner (D78, D79): the player only asks whether a
song has cues and reports when it has none; a report never slows or fails playback.

Spec: D79 (the player reports a no-cue song to the cue owner); D82 (once per song per app
run, through ``NoCueMemory``); D87(b)/(c) (one request at a time on the reporter's own worker
thread, at most 16 waiting, the rest dropped). The reporter knows only a callable: services
never import tasks, and the composition root hands it the cue owner's request.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import Future
from functools import partial
from queue import SimpleQueue
from threading import Thread
from typing import Protocol
from uuid import UUID

import structlog

from backend.domain.streaming import InvalidStreamValueError

logger = structlog.get_logger()


class NoCueReports(Protocol):
    """Where the player sends the files that played without cues."""

    def report(self, file_id: UUID) -> None:
        """Tell the cue owner a file played without cues. Called on the event loop; returns at
        once and never raises."""
        ...

    async def drained(self) -> None:
        """Return once no report waits or is being sent."""
        ...


class IgnoredReports:
    """The null object for the D2 rigs, which build the stream ports without a cue owner."""

    def report(self, file_id: UUID) -> None:
        """Do nothing: no cue owner listens."""

    async def drained(self) -> None:
        """Return at once: nothing is ever waiting."""


class NoCueMemory:
    """The files already reported this app run, at most ``limit``, the oldest forgotten first.

    Not thread-safe: event loop only.
    """

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise InvalidStreamValueError(f"NoCueMemory.limit must be >= 1, got {limit}")
        self._limit = limit
        self._files: OrderedDict[UUID, None] = OrderedDict()

    def seen(self, file_id: UUID) -> bool:
        """Whether this file was remembered and not yet forgotten."""
        return file_id in self._files

    def remember(self, file_id: UUID) -> None:
        """Remember a file, forgetting the oldest once past the limit."""
        self._files[file_id] = None
        if len(self._files) > self._limit:
            self._files.popitem(last=False)


type _Call = tuple[Callable[[], None], Future[None]]


class _DaemonWorker:
    """One daemon thread that runs calls one at a time, oldest first, started on first use.

    A daemon, unlike a ``ThreadPoolExecutor`` worker, which the interpreter joins at exit: a
    call still running when the app stops never holds the process (review I1).
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._calls: SimpleQueue[_Call | None] = SimpleQueue()
        self._thread: Thread | None = None

    def submit(self, call: Callable[[], None]) -> Future[None]:
        """Queue ``call``; the future settles with its outcome once it has run."""
        done: Future[None] = Future()
        self._calls.put((call, done))
        if self._thread is None:
            self._thread = Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()
        return done

    def stop(self) -> None:
        """Let the thread end once the call it may be running returns."""
        self._calls.put(None)

    def _run(self) -> None:
        """Run the queued calls until ``stop``, handing each outcome to its future."""
        while (queued := self._calls.get()) is not None:
            call, done = queued
            if not done.set_running_or_notify_cancel():
                continue  # its waiter was cancelled before it ran
            try:
                call()
            except Exception as error:  # noqa: BLE001 - handed to the waiter, which logs it
                done.set_exception(error)
            else:
                done.set_result(None)


class CueReporter:
    """Hands no-cue reports to the cue owner one at a time on its own worker thread, so a report
    never slows or fails playback (D79). Event loop only.

    The worker is the reporter's own daemon thread: one Huey SQLite connection, no thread taken
    from the default executor the day reads use, one request at a time by construction. No
    loop is bound and no thread started at construction, so a sync caller can build it.
    """

    def __init__(self, request: Callable[[UUID], None], pending: int = 16) -> None:
        if pending < 1:
            raise InvalidStreamValueError(f"CueReporter.pending must be >= 1, got {pending}")
        self._request = request
        self._pending = pending
        self._waiting: deque[UUID] = deque()
        self._drain: asyncio.Task[None] | None = None
        self._failing = False
        self._closed = False
        self._worker = _DaemonWorker("cue-reports")

    def report(self, file_id: UUID) -> None:
        """Queue a report for the worker, or drop it when ``pending`` already wait or the
        reporter is closed. Never awaits and never raises; E1's backlog still reaches a dropped
        file."""
        if self._closed:
            logger.debug("stream_cue_request_after_close", file_id=str(file_id))
            return
        if len(self._waiting) >= self._pending:
            logger.debug("stream_cue_request_dropped", file_id=str(file_id))
            return
        self._waiting.append(file_id)
        if self._drain is None or self._drain.done():
            self._drain = asyncio.get_running_loop().create_task(self._send_waiting())

    async def drained(self) -> None:
        """Return once nothing waits and no request runs. Waits without cancelling: cancelling
        this call never cancels a report being sent."""
        while self._drain is not None and not self._drain.done():
            await asyncio.wait({self._drain})

    def close(self) -> None:
        """Stop sending, without waiting (carried from Task 6a, review I1): the reports still
        waiting are dropped (the flush already logged them; the backlog covers them, D87(b)),
        later reports are ignored, and the worker thread ends after the request it may be
        running. That thread is a daemon, so a hung request never delays the app's exit."""
        if self._closed:
            return
        self._closed = True
        if self._waiting:
            logger.debug("stream_cue_requests_dropped_at_close", count=len(self._waiting))
            self._waiting.clear()
        self._worker.stop()

    async def _send_waiting(self) -> None:
        """Send the waiting reports, oldest first, one at a time on the worker thread, until
        none wait or the reporter is closed."""
        while self._waiting and not self._closed:
            await self._send(self._waiting.popleft())

    async def _send(self, file_id: UUID) -> None:
        """Send one report on the worker thread; a failure is logged, never raised."""
        try:
            await asyncio.wrap_future(self._worker.submit(partial(self._request, file_id)))
        except Exception as error:  # noqa: BLE001 - fire-and-forget boundary
            self._log_failure(file_id, error)
        else:
            self._failing = False

    def _log_failure(self, file_id: UUID, error: Exception) -> None:
        """Warn on the first failure after a success (or before any request ran), then debug,
        so a lasting failure does not flood System Logs."""
        log = logger.debug if self._failing else logger.warning
        log("stream_cue_request_failed", file_id=str(file_id), error=repr(error))
        self._failing = True

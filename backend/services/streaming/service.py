"""The stream service: admission, placement, the internal item contract, bookmarks and the
freeze watchdog (spec: Service and routes; Errors and edge cases; the Backend <-> Liquidsoap
contract; D10, D11, D15, D22-D32, D36, D39, D42, D43; R1: the day read overlaps the engine
start).

One instance per app, built at the composition root. Repository reads run in worker threads;
all session state is mutated on the event loop (the day memo is the one exception, and it
carries its own lock).
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import structlog

from backend.domain.streaming import (
    Bookmark,
    DayLoader,
    EndOfScheduleError,
    InvalidStreamValueError,
    ItemRef,
    Landing,
    ScheduleItem,
    StreamTiming,
    TuneIn,
)
from backend.domain.tune_in import next_item, resume, tune_in
from backend.playout.harbor import Upstream
from backend.playout.liquidsoap_process import (
    EngineStartError,
    RunningEngine,
    SessionEndpoint,
    free_port,
)
from backend.playout.relay import AsgiApp, RelayConfig, client_framing, relay_asgi
from backend.repositories.broadcast_stations import BroadcastStationRepository
from backend.repositories.playable_schedule import PlayableScheduleRepository
from backend.repositories.user_settings import UserSettingRepository
from backend.services.streaming.bookmarks import (
    BookmarkKey,
    BookmarkStore,
    SavedBookmark,
    bookmark_still_valid,
)
from backend.services.streaming.errors import (
    InvalidStreamSettingError,
    SessionTokenError,
    StationBusyError,
    StationNotFoundError,
    StreamUnavailableError,
    UnknownItemError,
    UnknownSessionError,
)
from backend.services.streaming.max_sessions import MAX_SESSIONS_KEY, parse_max_sessions
from backend.services.streaming.payload import FinalClip, ItemPayload, final_payload, item_payload
from backend.services.streaming.sessions import (
    Assigned,
    Committed,
    StreamSession,
    memoised_day_loader,
)
from backend.services.streaming.watchdog import PlayingSpan, freeze_deadline

__all__ = [
    "ItemCall",
    "ListenRequest",
    "OpenedStream",
    "ReposFactory",
    "StreamPorts",
    "StreamRepos",
    "StreamService",
    "StreamServiceConfig",
]

logger = structlog.get_logger()

_INTERNAL_PATH = "/internal/stream/sessions"
_PLACEMENT_WAIT_S = 5.0
"""How long seq 0 waits for placement: below Liquidsoap's 10 s ``http.get`` timeout."""


@dataclass(frozen=True)
class StreamRepos:
    """The repository ports the service reads; all synchronous (run them in a thread)."""

    stations: BroadcastStationRepository
    settings: UserSettingRepository
    schedule: PlayableScheduleRepository


type ReposFactory = Callable[[], AbstractContextManager[StreamRepos]]
"""Opens the repositories for one use (one connection in production)."""


@dataclass(frozen=True)
class StreamPorts:
    """What the service is wired to at the composition root."""

    repos: ReposFactory
    start_engine: Callable[[SessionEndpoint], Awaitable[RunningEngine]]
    clock: Callable[[], datetime]
    steady: Callable[[], float] | None = None
    """Seconds on a monotonic clock (``time.monotonic``) for everything that measures elapsed
    time (D47); ``clock`` then only places listeners. ``None`` measures elapsed time on
    ``clock`` too, which is how the D2 rigs drive every duration with one clock."""


_STEADY_EPOCH = datetime(2000, 1, 1)
"""Steady readings become datetimes from this arbitrary origin, so the elapsed-time fields
keep their D2 types; they are only ever compared with each other, never with the wall."""


def _elapsed_clock(ports: StreamPorts) -> Callable[[], datetime]:
    """The clock durations are measured on: the steady clock when wired, else the wall."""
    steady = ports.steady
    if steady is None:
        return ports.clock
    return lambda: _STEADY_EPOCH + timedelta(seconds=steady())


@dataclass(frozen=True)
class StreamServiceConfig:
    """Where engines call back and log, and the stream's timing rules."""

    callback_base_url: str
    log_dir: Path
    final_clip: FinalClip | None = None
    timing: StreamTiming = field(default_factory=StreamTiming)
    relay: RelayConfig = field(default_factory=RelayConfig)
    freeze_grace: timedelta = timedelta(seconds=30)  # D31
    now_playing_delay: timedelta = timedelta(seconds=1)  # contract: "~1 s delay"


@dataclass(frozen=True)
class ListenRequest:
    """One tune-in: a station-year, the listener's bookmark key (D28) and the ICY header."""

    call_letters: str
    year: int
    listener_key: str | None
    icy_metadata: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.year <= 9999:
            raise InvalidStreamValueError(f"ListenRequest.year must be 1..9999, got {self.year}")
        if self.listener_key == "":
            raise InvalidStreamValueError("ListenRequest.listener_key must not be empty")


@dataclass(frozen=True)
class ItemCall:
    """One call from a session engine: which session, the token it sent, which item."""

    session_id: str
    token: str | None
    seq: int


@dataclass(frozen=True)
class OpenedStream:
    """An admitted, placed and started session, and the ASGI app that relays its audio."""

    session_id: str
    app: AsgiApp


# ---- worker-thread steps (repository reads and the domain's schedule walks) ----------------


def _station_and_limit(repos: ReposFactory, call_letters: str) -> tuple[UUID | None, str | None]:
    """The station with exactly these call letters (D36), and the raw listener limit."""
    with repos() as opened:
        station = opened.stations.get_by_call_letters(call_letters)
        setting = opened.settings.get(MAX_SESSIONS_KEY)
    return (
        None if station is None else station.id,
        None if setting is None else setting.value,
    )


def _day_loader(repos: ReposFactory, station_id: UUID) -> DayLoader:
    """This station's days, each read at most once for the session (carried item C9)."""

    def read_day(day: date) -> Sequence[ScheduleItem]:
        with repos() as opened:
            return opened.schedule.get_day(station_id, day)

    return memoised_day_loader(read_day)


@dataclass(frozen=True)
class _Schedule:
    """What a placement reads: the session's days, its year, the moment and the rules.

    ``now`` is the wall clock, which places the listener; ``resumed_at`` is the bookmark's
    ``left_at`` plus the steady time away (D47), the moment a resume walks forward to.
    """

    load_day: DayLoader
    year: int
    now: datetime
    resumed_at: datetime
    timing: StreamTiming


def _place(schedule: _Schedule, saved: SavedBookmark | None, forget: Callable[[], None]) -> TuneIn:
    """Resume a still-valid bookmark, else tune in by the clock (D11, D28).

    ``forget`` drops the bookmark: when it is no longer valid, and when the resume walked
    past the end of the log (D26 with D39, D43).
    """
    load_day, now, timing = schedule.load_day, schedule.now, schedule.timing
    if saved is not None and bookmark_still_valid(load_day, saved, now):
        try:
            landing = resume(load_day, saved.bookmark, schedule.resumed_at, timing)
        except EndOfScheduleError:
            forget()  # the station has signed off since the listener left
            raise
        return TuneIn(landing, saved.bookmark.clock_offset)
    if saved is not None:
        forget()  # no longer worth resuming from
    return tune_in(load_day, schedule.year, now, timing)


def _item_at(load_day: DayLoader, ref: ItemRef) -> ScheduleItem:
    return load_day(ref.day)[ref.index]


def _landing_assigned(load_day: DayLoader, landing: Landing) -> Assigned:
    return Assigned(landing.ref, _item_at(load_day, landing.ref), landing.offset_ms)


def _following(load_day: DayLoader, after: ItemRef, timing: StreamTiming) -> Assigned:
    """The next playable item after ``after``, from its top (D39: at most one day ahead)."""
    ref = next_item(load_day, after, timing)
    return Assigned(ref, _item_at(load_day, ref), 0)


# ---- pure helpers ---------------------------------------------------------------------------


def _payload(seq: int, assigned: Assigned | FinalClip, timing: StreamTiming) -> ItemPayload:
    if isinstance(assigned, FinalClip):
        return final_payload(seq, assigned)
    return item_payload(seq, assigned.item, assigned.offset_ms, timing)


def _flagged(assigned: Assigned | FinalClip) -> dict[str, str | None]:
    """The fields a failed item is flagged with (D32): its play, file and path."""
    if isinstance(assigned, FinalClip):
        return {"event_id": None, "file_id": None, "path": str(assigned.path)}
    file = assigned.item.file
    return {
        "event_id": str(assigned.item.event_id),
        "file_id": None if file is None else str(file.file_id),
        "path": None if file is None else file.path,
    }


def _token_matches(sent: str | None, expected: str) -> bool:
    """Constant-time comparison; a missing token never matches."""
    return sent is not None and secrets.compare_digest(sent.encode(), expected.encode())


def _playing_span(committed: Committed | None) -> PlayingSpan | None:
    """What is playing now and how long it has left, for the freeze deadline."""
    if committed is None:
        return None
    playing = committed.assigned
    if isinstance(playing, FinalClip):
        return PlayingSpan(committed.started_at, playing.span_ms)
    span_ms = 0 if playing.item.file is None else playing.item.file.span_ms()
    return PlayingSpan(committed.started_at, span_ms - playing.offset_ms)


def _left_at(session: StreamSession, now: datetime, elapsed_now: datetime) -> SavedBookmark | None:
    """Where the listener was at ``now``: the committed item plus the time it has played
    (D11, measured on the elapsed clock: D47), or the landing when nothing has started
    (D30); ``None`` if the session never finished opening (no engine), so a close during
    ``open`` bookmarks nothing."""
    if session.engine is None or session.clock_offset is None or session.landing is None:
        return None
    committed = session.committed
    if committed is not None and isinstance(committed.assigned, Assigned):
        playing = committed.assigned
        heard = elapsed_now - committed.started_at
        landing = Landing(playing.ref, playing.offset_ms).advanced_by(heard)
        item = playing.item
    else:
        landing = session.landing
        item = _landing_item(session, landing)
    bookmark = Bookmark(landing, item.logged_at, now, session.clock_offset)
    return SavedBookmark(bookmark, item.event_id, left_elapsed=elapsed_now)


def _landing_item(session: StreamSession, landing: Landing) -> ScheduleItem:
    """The landing's play: seq 0's item once it was sent, else the memo (placement read the
    day, so this is a hit; it still takes the memo's lock, hence the preference)."""
    first = session.assigned.get(0)
    if isinstance(first, Assigned):
        return first.item
    return _item_at(session.load_day, landing.ref)


def _schedule_finished(session: StreamSession) -> bool:
    """The schedule has ended and its last assigned item has started (D26)."""
    committed = session.committed
    return (
        session.end_seq is not None
        and committed is not None
        and committed.seq == session.end_seq - 1
    )


def _halt(engine: RunningEngine) -> None:
    """Kill the engine without waiting (D1) and close its upstream (``stop`` does not)."""
    engine.stop()
    engine.upstream.close()


def _closing_once(engine: RunningEngine) -> RunningEngine:
    """``engine`` with an upstream whose ``close`` runs once: the relay closes it when the
    response ends, and the halt of the session's close that follows must not close it again."""
    upstream = engine.upstream
    closed = False

    def close_once() -> None:
        nonlocal closed
        if not closed:
            closed = True
            upstream.close()

    return replace(engine, upstream=Upstream(read=upstream.read, close=close_once))


def _halt_if_started(starting: asyncio.Future[RunningEngine]) -> None:
    """Halt the engine a finished start produced (a done-callback, like D1's
    ``_abandon_started``); a cancelled or failed start left nothing running."""
    if not starting.cancelled() and starting.exception() is None:
        _halt(starting.result())


def _abandon_start(starting: asyncio.Future[RunningEngine]) -> None:
    """An open that will not keep its engine: halt it now if ready, else once it is."""
    if starting.done():
        _halt_if_started(starting)
    else:
        starting.add_done_callback(_halt_if_started)


class StreamService:
    """Every open listener session of this app; the table's size is the slot count (D42)."""

    def __init__(
        self, ports: StreamPorts, bookmarks: BookmarkStore, config: StreamServiceConfig
    ) -> None:
        self._ports = ports
        self._bookmarks = bookmarks
        self._config = config
        self._elapsed = _elapsed_clock(ports)
        self._sessions: dict[str, StreamSession] = {}

    # ---- queries ---------------------------------------------------------------------------

    @property
    def open_sessions(self) -> int:
        return len(self._sessions)

    def now_playing(self, session_id: str) -> str:
        """``artist - title`` once its delay has passed; ``""`` before that, or if unknown."""
        session = self._sessions.get(session_id)
        if session is None or session.now_playing is None:
            return ""
        shows_at, text = session.now_playing
        return text if self._elapsed() >= shows_at else ""

    def frozen_sessions(self) -> list[str]:
        """Running sessions whose next ``started`` report is overdue (D31)."""
        now = self._elapsed()
        grace = self._config.freeze_grace
        return [
            session_id
            for session_id, session in self._sessions.items()
            if session.engine is not None
            and not session.stopped
            and now >= freeze_deadline(session.opened_at, _playing_span(session.committed), grace)
        ]

    # ---- open ------------------------------------------------------------------------------

    async def open(self, request: ListenRequest) -> OpenedStream:
        """Admit, place and start one listener's session; the handle relays its audio."""
        station_id, raw_limit = await asyncio.to_thread(
            _station_and_limit, self._ports.repos, request.call_letters
        )
        if station_id is None:
            raise StationNotFoundError(f"no station has the call letters {request.call_letters!r}")
        limit = self._listener_limit(raw_limit)
        session_id, session = self._admit(station_id, limit, request)  # no await before this
        opened = False
        try:
            engine = await self._place_while_starting(session_id, session, request.year)
            app = self._relay_app(session_id, engine, request.icy_metadata)
            opened = True
        finally:
            if not opened:
                self._stop(session)  # an engine settled before _relay_app raised is halted
                self._sessions.pop(session_id, None)
                session.placed.set()  # wake a waiting seq 0 request: the session is gone
        return OpenedStream(session_id, app)

    def _listener_limit(self, raw: str | None) -> int:
        try:
            return parse_max_sessions(raw)
        except InvalidStreamSettingError:
            logger.error("stream_setting_invalid", setting=MAX_SESSIONS_KEY, value=raw)  # D27
            raise

    def _admit(
        self, station_id: UUID, limit: int, request: ListenRequest
    ) -> tuple[str, StreamSession]:
        """Take a slot, atomically: nothing awaits between the check and the insert (D42)."""
        if len(self._sessions) >= limit:
            raise StationBusyError(f"all {limit} listener slots are taken")
        key = request.listener_key
        session = StreamSession(
            load_day=_day_loader(self._ports.repos, station_id),
            opened_at=self._elapsed(),
            bookmark_key=None if key is None else BookmarkKey(key, station_id, request.year),
            token=secrets.token_urlsafe(32),
        )
        session_id = uuid4().hex
        self._sessions[session_id] = session
        return session_id, session

    def _endpoint(self, session_id: str, token: str) -> SessionEndpoint:
        return SessionEndpoint(
            session_id=session_id,
            harbor_port=free_port(),
            backend_url=f"{self._config.callback_base_url}{_INTERNAL_PATH}/{session_id}",
            session_token=token,
            log_path=self._config.log_dir / f"{session_id}.log",
        )

    async def _place_while_starting(
        self, session_id: str, session: StreamSession, year: int
    ) -> RunningEngine:
        """Place the listener in a worker thread while the engine starts (R1).

        An engine this open does not keep, because placement failed, the start failed, the
        session was closed, or the open was cancelled, is halted whenever it becomes ready.
        """
        endpoint = self._endpoint(session_id, session.token)
        starting = asyncio.ensure_future(self._ports.start_engine(endpoint))
        placing = self._publish_placement(session, year)
        kept = False
        try:
            placed, started = await asyncio.gather(placing, starting, return_exceptions=True)
            engine = self._settle(session_id, session, placed, started)
            kept = True
        finally:
            if not kept:
                _abandon_start(starting)
        return engine

    async def _publish_placement(self, session: StreamSession, year: int) -> None:
        """Place the listener and publish the landing at once, so the engine's seq 0 request
        need not wait for the engine start to be confirmed; ``placed`` is set either way."""
        key = session.bookmark_key
        saved = None if key is None else self._bookmarks.get(key)
        schedule = _Schedule(
            session.load_day,
            year,
            self._ports.clock(),
            self._resumed_at(saved),
            self._config.timing,
        )
        try:
            tuned = await asyncio.to_thread(_place, schedule, saved, self._forgetter(key, saved))
            session.landing, session.clock_offset = tuned.landing, tuned.clock_offset
        finally:
            session.placed.set()

    def _resumed_at(self, saved: SavedBookmark | None) -> datetime:
        """The bookmark's ``left_at`` plus the steady time away (D11, D47); the wall clock
        when there is no bookmark, or it carries no elapsed-clock reading."""
        if saved is None or saved.left_elapsed is None:
            return self._ports.clock()
        return saved.bookmark.left_at + (self._elapsed() - saved.left_elapsed)

    def _forgetter(
        self, key: BookmarkKey | None, saved: SavedBookmark | None
    ) -> Callable[[], None]:
        """Drops ``saved`` when called from the placement thread; the store changes on the
        event loop, and only if no newer bookmark has replaced ``saved`` meanwhile."""
        loop = asyncio.get_running_loop()

        def forget_on_loop() -> None:
            if key is not None and saved is not None and self._bookmarks.get(key) is saved:
                self._bookmarks.delete(key)

        def forget() -> None:
            loop.call_soon_threadsafe(forget_on_loop)

        return forget

    def _settle(
        self,
        session_id: str,
        session: StreamSession,
        placed: None | BaseException,
        started: RunningEngine | BaseException,
    ) -> RunningEngine:
        """Keep the engine, or fail: a placement failure wins. The caller halts an engine
        that is not kept."""
        if isinstance(placed, BaseException):
            raise placed
        if isinstance(started, EngineStartError):
            raise StreamUnavailableError(
                f"session {session_id}: the engine did not start"
            ) from started
        if isinstance(started, BaseException):
            raise started
        if self._sessions.get(session_id) is not session:  # closed (close_all) while opening
            raise StreamUnavailableError(f"session {session_id}: closed while opening")
        kept = _closing_once(started)  # the relay and every halt share its one close
        session.engine = kept
        return kept

    def _relay_app(
        self, session_id: str, engine: RunningEngine, icy_metadata: str | None
    ) -> AsgiApp:
        relay = self._config.relay
        framing = client_framing(icy_metadata, lambda: self.now_playing(session_id), relay)
        return relay_asgi(engine.upstream, framing, lambda: self.close(session_id), relay)

    # ---- the item contract -------------------------------------------------------------------

    async def item(self, call: ItemCall) -> ItemPayload:
        """The item for ``call.seq``, assigned on its first request and the same every time
        after ("the same seq always returns the same item")."""
        session = self._authorised(call)
        served = self._served(session, call.seq)
        if served is not None:
            return served
        if call.seq == 0:
            await self._await_placement(call, session)
            session = self._authorised(call)  # a failed open has removed the session
            landing = await asyncio.to_thread(
                _landing_assigned, session.load_day, self._placed(call, session)
            )
            return self._assign(self._authorised(call), 0, landing)
        previous = session.assigned[call.seq - 1]
        if isinstance(previous, FinalClip):  # the end marker already follows the clip
            raise EndOfScheduleError(f"session {call.session_id}: the final clip was the last")
        timing = self._config.timing
        try:
            following = await asyncio.to_thread(_following, session.load_day, previous.ref, timing)
        except EndOfScheduleError:
            # The domain's documented end of the schedule (D26, D39): recorded, then answered.
            clip = self._end_schedule(self._authorised(call), call.seq)
            if clip is None:
                raise
            return clip
        return self._assign(self._authorised(call), call.seq, following)

    def _authorised(self, call: ItemCall) -> StreamSession:
        session = self._sessions.get(call.session_id)
        if session is None:
            raise UnknownSessionError(f"no open session {call.session_id}")
        if not _token_matches(call.token, session.token):
            raise SessionTokenError(f"session {call.session_id}: wrong or missing token")
        return session

    def _served(self, session: StreamSession, seq: int) -> ItemPayload | None:
        """The payload already assigned to ``seq``; ``None`` when ``seq`` is the next one."""
        assigned = session.assigned.get(seq)
        if assigned is not None:
            return _payload(seq, assigned, self._config.timing)
        if session.end_seq is not None and seq >= session.end_seq:
            raise EndOfScheduleError(f"the schedule ended at item {session.end_seq}")
        if seq != len(session.assigned):
            raise UnknownItemError(f"item {seq} is not next; {len(session.assigned)} assigned")
        return None

    async def _await_placement(self, call: ItemCall, session: StreamSession) -> None:
        """Hold the engine's seq 0 request until placement ends (it usually arrives while the
        day is still being read); past the wait, the engine is told to retry later."""
        try:
            async with asyncio.timeout(_PLACEMENT_WAIT_S):
                await session.placed.wait()
        except TimeoutError as timed_out:
            raise UnknownItemError(
                f"session {call.session_id}: not placed after {_PLACEMENT_WAIT_S} s"
            ) from timed_out

    def _placed(self, call: ItemCall, session: StreamSession) -> Landing:
        """seq 0 is the landing; a session whose placement failed has none."""
        if session.landing is None:
            raise UnknownItemError(f"session {call.session_id}: not placed")
        return session.landing

    def _assign(self, session: StreamSession, seq: int, assigned: Assigned) -> ItemPayload:
        served = self._served(session, seq)  # a concurrent request may have assigned it
        if served is not None:
            return served
        payload = _payload(seq, assigned, self._config.timing)
        session.assigned[seq] = assigned
        return payload

    def _end_schedule(self, session: StreamSession, seq: int) -> ItemPayload | None:
        """Record the end at ``seq``: the final clip's payload, or ``None`` with no clip."""
        served = self._served(session, seq)  # a concurrent request may have recorded it
        if served is not None:
            return served
        clip = self._config.final_clip
        if clip is None:
            session.end_seq = seq
            return None
        session.assigned[seq] = clip
        session.end_seq = seq + 1
        return final_payload(seq, clip)

    # ---- reports ------------------------------------------------------------------------------

    def started(self, call: ItemCall) -> None:
        """Commit the position (never backwards) and time the title; then run the watchdog."""
        session = self._authorised(call)
        assigned = self._reported(call, session)
        committed = session.committed
        if committed is None or call.seq > committed.seq:
            now = self._elapsed()
            session.committed = Committed(call.seq, assigned, now)
            if isinstance(assigned, Assigned):  # the final clip has no title
                title = f"{assigned.item.artist} - {assigned.item.title}"
                session.now_playing = (now + self._config.now_playing_delay, title)
        self.stop_frozen()  # spec: the watchdog runs "on each started"

    def failed(self, call: ItemCall) -> None:
        """Flag an item the engine could not play (D32); the engine plays the next one."""
        assigned = self._reported(call, self._authorised(call))
        logger.warning(
            "stream_item_failed", session_id=call.session_id, seq=call.seq, **_flagged(assigned)
        )

    def _reported(self, call: ItemCall, session: StreamSession) -> Assigned | FinalClip:
        assigned = session.assigned.get(call.seq)
        if assigned is None:
            raise UnknownItemError(f"session {call.session_id}: item {call.seq} was not sent")
        return assigned

    # ---- close and the watchdog --------------------------------------------------------------

    def close(self, session_id: str) -> None:
        """Free the slot, stop the engine without waiting, and keep or clear the bookmark."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        self._stop(session)
        key = session.bookmark_key
        if key is None:
            return  # D28
        if _schedule_finished(session):
            self._bookmarks.delete(key)  # D26
            return
        now = self._ports.clock()
        saved = _left_at(session, now, self._elapsed())
        if saved is not None:
            self._bookmarks.put(key, saved, now)

    def close_all(self) -> None:
        for session_id in list(self._sessions):
            self.close(session_id)

    def stop_frozen(self) -> None:
        """Stop every frozen session's engine; its relay then ends and closes the session."""
        for session_id in self.frozen_sessions():
            self._stop(self._sessions[session_id])
            logger.warning("stream_session_frozen", session_id=session_id)

    def _stop(self, session: StreamSession) -> None:
        if session.engine is not None and not session.stopped:
            _halt(session.engine)
            session.stopped = True

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
from dataclasses import dataclass, field
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


def _place(
    load_day: DayLoader,
    year: int,
    saved: SavedBookmark | None,
    now: datetime,
    timing: StreamTiming,
    forget: Callable[[], None],
) -> TuneIn:
    """Resume a still-valid bookmark, else tune in by the clock (D11, D28).

    ``forget`` drops the bookmark: when it is no longer valid, and when the resume walked
    past the end of the log (D26 with D39, D43).
    """
    if saved is None:
        return tune_in(load_day, year, now, timing)
    if not bookmark_still_valid(load_day, saved, now):
        forget()
        return tune_in(load_day, year, now, timing)
    try:
        landing = resume(load_day, saved.bookmark, now, timing)
    except EndOfScheduleError:
        forget()  # the station has signed off since the listener left
        raise
    return TuneIn(landing, saved.bookmark.clock_offset)


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


def _left_at(session: StreamSession, now: datetime) -> SavedBookmark | None:
    """Where the listener was at ``now``: the committed item plus the time it has played
    (D11), or the landing when nothing has started (D30); ``None`` if never placed."""
    if session.clock_offset is None or session.landing is None:
        return None
    committed = session.committed
    if committed is not None and isinstance(committed.assigned, Assigned):
        playing = committed.assigned
        landing = Landing(playing.ref, playing.offset_ms).advanced_by(now - committed.started_at)
        item = playing.item
    else:
        landing = session.landing
        item = _item_at(session.load_day, landing.ref)  # read by placement: a memo hit
    bookmark = Bookmark(landing, item.logged_at, now, session.clock_offset)
    return SavedBookmark(bookmark, item.event_id)


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


class StreamService:
    """Every open listener session of this app; the table's size is the slot count (D42)."""

    def __init__(
        self, ports: StreamPorts, bookmarks: BookmarkStore, config: StreamServiceConfig
    ) -> None:
        self._ports = ports
        self._bookmarks = bookmarks
        self._config = config
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
        return text if self._ports.clock() >= shows_at else ""

    def frozen_sessions(self) -> list[str]:
        """Running sessions whose next ``started`` report is overdue (D31)."""
        now = self._ports.clock()
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
                self._sessions.pop(session_id, None)
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
            opened_at=self._ports.clock(),
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
        """Place the listener in a worker thread while the engine starts (R1)."""
        key = session.bookmark_key
        saved = None if key is None else self._bookmarks.get(key)
        forget = self._forgetter(key, saved)
        now, timing = self._ports.clock(), self._config.timing
        placing = asyncio.to_thread(_place, session.load_day, year, saved, now, timing, forget)
        starting = self._ports.start_engine(self._endpoint(session_id, session.token))
        placed, started = await asyncio.gather(placing, starting, return_exceptions=True)
        return self._settle(session_id, session, placed, started)

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
        placed: TuneIn | BaseException,
        started: RunningEngine | BaseException,
    ) -> RunningEngine:
        """Keep the placement and the engine, or fail: a placement failure wins."""
        if isinstance(placed, BaseException):
            if isinstance(started, RunningEngine):
                _halt(started)
            raise placed
        if isinstance(started, EngineStartError):
            raise StreamUnavailableError(
                f"session {session_id}: the engine did not start"
            ) from started
        if isinstance(started, BaseException):
            raise started
        if self._sessions.get(session_id) is not session:  # closed (close_all) while opening
            _halt(started)
            raise StreamUnavailableError(f"session {session_id}: closed while opening")
        session.landing, session.clock_offset = placed.landing, placed.clock_offset
        session.engine = started
        return started

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

    def _placed(self, call: ItemCall, session: StreamSession) -> Landing:
        """seq 0 is the landing; until placement finishes, the engine must retry later."""
        if session.landing is None:
            raise UnknownItemError(f"session {call.session_id}: not placed yet")
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
            now = self._ports.clock()
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
        saved = _left_at(session, now)
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

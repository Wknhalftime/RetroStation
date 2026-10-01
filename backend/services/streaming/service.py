"""The stream service: admission, placement, the internal item contract, bookmarks and the
freeze watchdog (spec: Service and routes; Errors and edge cases; the Backend <-> Liquidsoap
contract; D10, D11, D15, D22-D32, D39, D42, D43, D72; R1: the day read overlaps the engine
start); and what it tells the listener feed (D13, D28, D74, D78a, D78b).

Songs without cues (D78-D80, D85): each song after the landing has its stored cues re-read
just before it is handed out, within a time limit, and keeps what was read at tune-in when
that read fails (D85). A song handed out without cues plays D23's values, is warned about
and reported to the cue owner once per app run (D78, D79, D82); a landing whose tail is
too short for its cues plays D23's values with a warning (D80).

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
from functools import partial
from pathlib import Path
from uuid import UUID, uuid4

import structlog

from backend.domain.streaming import (
    Bookmark,
    CuePoints,
    DayLoader,
    EndOfScheduleError,
    InvalidStreamValueError,
    ItemRef,
    Landing,
    NoBroadcastError,
    PlayableFile,
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
from backend.services.streaming.cue_reports import IgnoredReports, NoCueMemory, NoCueReports
from backend.services.streaming.cue_reread import CueRereadLimits, CueRereads, Unread
from backend.services.streaming.errors import (
    InvalidStreamSettingError,
    SessionTokenError,
    StationBusyError,
    StationNotFoundError,
    StreamUnavailableError,
    UnknownItemError,
    UnknownSessionError,
)
from backend.services.streaming.listener_events import (
    ListenerEvents,
    ListenerFeed,
    NowPlaying,
    Sleep,
    StatusKind,
)
from backend.services.streaming.max_sessions import MAX_SESSIONS_KEY, parse_max_sessions
from backend.services.streaming.payload import (
    FinalClip,
    ItemPayload,
    final_payload,
    landing_payload,
)
from backend.services.streaming.sessions import (
    Assigned,
    Committed,
    StreamSession,
    memoised_day_loader,
)
from backend.services.streaming.watchdog import PlayingSpan, freeze_deadline

__all__ = [
    "EventsRequest",
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
_SUBSCRIPTIONS_PER_SLOT = 2
"""Now-playing subscriptions allowed per listener slot, app-wide (D78b with D42)."""
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
    cue_reread_repos: ReposFactory | None = None
    """The re-read's own connections (D85), with short lock and statement limits; ``None``
    re-reads through ``repos``, as the D2 rigs do."""
    sleep: Sleep = asyncio.sleep
    """Waits on the loop's monotonic clock (D47); the 1 s now-playing delay uses it. Tests
    inject a gate."""
    cue_reports: NoCueReports = field(default_factory=IgnoredReports)
    """Where a song sent without cues is reported (D79); ``IgnoredReports`` in the D2 rigs."""


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
    no_cue_memory: int = 4096
    """How many files a run of the app remembers having reported (D82, D87(c))."""
    cue_reread: CueRereadLimits = field(default_factory=CueRereadLimits)
    """How long a song's stored-cue re-read may take and how many run at once (D85, D86(c))."""

    def __post_init__(self) -> None:
        if self.no_cue_memory < 1:
            raise InvalidStreamValueError(
                f"StreamServiceConfig.no_cue_memory must be >= 1, got {self.no_cue_memory}"
            )


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
class EventsRequest:
    """One page's now-playing subscription: a station-year and the listener's key (D28: no
    key, no events)."""

    call_letters: str
    year: int
    listener_key: str

    def __post_init__(self) -> None:
        if not 1 <= self.year <= 9999:
            raise InvalidStreamValueError(f"EventsRequest.year must be 1..9999, got {self.year}")
        if self.listener_key == "":
            raise InvalidStreamValueError("EventsRequest.listener_key must not be empty")


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
    """The station with these call letters, in any case (D72), and the raw listener limit."""
    with repos() as opened:
        station = opened.stations.get_by_call_letters(call_letters)
        setting = opened.settings.get(MAX_SESSIONS_KEY)
    return (
        None if station is None else station.id,
        None if setting is None else setting.value,
    )


def _channel(request: ListenRequest, station_id: UUID) -> BookmarkKey | None:
    """The tune-in's bookmark key, which is also its listener channel (D74); keyless, none
    (D28)."""
    key = request.listener_key
    return None if key is None else BookmarkKey(key, station_id, request.year)


def _day_loader(repos: ReposFactory, station_id: UUID) -> DayLoader:
    """This station's days, each read at most once for the session (carried item C9)."""

    def read_day(day: date) -> Sequence[ScheduleItem]:
        with repos() as opened:
            return opened.schedule.get_day(station_id, day)

    return memoised_day_loader(read_day)


def _stored_cues(repos: ReposFactory, file_id: UUID) -> CuePoints | None:
    """The cues stored for the file's audio now (D85); one connection per use."""
    with repos() as opened:
        return opened.schedule.file_cues(file_id)


@dataclass(frozen=True)
class _Schedule:
    """What a placement reads: the session's days, its year, the moment and the rules.

    ``now`` is the wall clock, which places the listener; ``resume_to`` is the bookmark's
    ``left_at`` plus the steady time away (D47), the moment a resume walks forward to,
    or ``None`` when there is no bookmark or it has no elapsed-clock reading.
    """

    load_day: DayLoader
    year: int
    now: datetime
    resume_to: datetime | None
    timing: StreamTiming


def _place(schedule: _Schedule, saved: SavedBookmark | None, forget: Callable[[], None]) -> TuneIn:
    """Resume a still-valid bookmark, else tune in by the clock (D11, D28).

    ``forget`` drops the bookmark: when it is no longer valid, and when the resume walked
    past the end of the log (D26 with D39, D43).
    """
    load_day, now, timing = schedule.load_day, schedule.now, schedule.timing
    if saved is not None and bookmark_still_valid(load_day, saved, now):
        try:
            walk_to = now if schedule.resume_to is None else schedule.resume_to
            landing = resume(load_day, saved.bookmark, walk_to, timing)
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
    """The item's annotations; an item's cues are dropped when its tail is too short for
    them (D80; a later item starts at offset 0, so only a landing can be affected)."""
    if isinstance(assigned, FinalClip):
        return final_payload(seq, assigned)
    return landing_payload(seq, assigned.item, assigned.offset_ms, timing)


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


def _left_at(
    session: StreamSession, *, wall_now: datetime, elapsed_now: datetime
) -> SavedBookmark | None:
    """Where the listener was at ``wall_now``: the committed item plus the time it has played
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
    bookmark = Bookmark(landing, item.logged_at, wall_now, session.clock_offset)
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
        self._feed = ListenerFeed(self._elapsed, ports.sleep)
        self._no_cues = NoCueMemory(config.no_cue_memory)
        reread_repos = ports.cue_reread_repos or ports.repos
        self._rereads = CueRereads(partial(_stored_cues, reread_repos), config.cue_reread)

    # ---- queries ---------------------------------------------------------------------------

    @property
    def open_sessions(self) -> int:
        return len(self._sessions)

    @property
    def event_channels(self) -> int:
        """Listener channels with a stream or a subscription."""
        return self._feed.channels

    def now_playing(self, session_id: str) -> str:
        """``artist - title`` once its delay has passed; ``""`` before that, or if unknown."""
        session = self._sessions.get(session_id)
        if session is None or session.now_playing is None:
            return ""
        shows_at, text = session.now_playing
        return text if self._elapsed() >= shows_at else ""

    def frozen_sessions(self) -> list[str]:
        """Running sessions whose next ``started`` report is overdue (D31)."""
        elapsed_now = self._elapsed()
        grace = self._config.freeze_grace
        return [
            session_id
            for session_id, session in self._sessions.items()
            if session.engine is not None
            and not session.stopped
            and elapsed_now
            >= freeze_deadline(session.opened_at, _playing_span(session.committed), grace)
        ]

    # ---- open ------------------------------------------------------------------------------

    async def open(self, request: ListenRequest) -> OpenedStream:
        """Admit, place and start one listener's session; the handle relays its audio. A
        keyed tune-in is told on its channel as it goes (D78a)."""
        station_id, raw_limit = await asyncio.to_thread(
            _station_and_limit, self._ports.repos, request.call_letters
        )
        if station_id is None:
            raise StationNotFoundError(f"no station has the call letters {request.call_letters!r}")
        channel, session_id, session = self._admit_told(
            station_id, raw_limit, request
        )  # no await before
        opened = False
        # D78b: an unexpected failure, including StreamUnavailableError, is never "stopped".
        failure = StatusKind.UNAVAILABLE
        try:
            if channel is not None:
                self._feed.opened(channel, session_id)  # D42: every exit from here frees the slot
            engine = await self._place_while_starting(session_id, session, request.year)
            app = self._relay_app(session_id, engine, request.icy_metadata)
            opened = True
        except (NoBroadcastError, EndOfScheduleError):
            failure = StatusKind.NO_BROADCAST
            raise
        except asyncio.CancelledError:
            failure = StatusKind.STOPPED  # the listener left mid-start (D6)
            raise
        finally:
            if not opened:
                self._abandon_open(session_id, session, failure)
        return OpenedStream(session_id, app)

    def _abandon_open(self, session_id: str, session: StreamSession, failure: StatusKind) -> None:
        """A session that never finished opening: told and forgotten before the halt, so a
        stop that raises (D78c review M1) can't leave it stuck in the feed or its waiter
        hanging. The halt's own failure is logged, not raised (D78c review M2): the error
        that failed the open is always the one the caller sees, never a secondary one from
        an engine that also would not stop."""
        self._sessions.pop(session_id, None)
        session.placed.set()  # wake a waiting seq 0 request: the session is gone
        self._tell_failure(session_id, session, failure)
        try:
            self._stop(session)  # an engine settled before _relay_app raised is halted
        except OSError:
            logger.exception("stream_open_abandon_stop_failed", session_id=session_id)

    def _admit_told(
        self, station_id: UUID, raw_limit: str | None, request: ListenRequest
    ) -> tuple[BookmarkKey | None, str, StreamSession]:
        """Take a slot under the parsed limit, telling the refusal (D27: a bad limit setting
        is ``unavailable``; D10: ``busy``) before it is raised. The caller tells ``tuning`` on
        the returned channel, inside its own try/finally, so the slot is freed if that fails."""
        channel = _channel(request, station_id)
        try:
            limit = self._listener_limit(raw_limit)
            session_id, session = self._admit(station_id, limit, request)
        except InvalidStreamSettingError:
            self._refuse(channel, StatusKind.UNAVAILABLE)
            raise
        except StationBusyError:
            self._refuse(channel, StatusKind.BUSY)
            raise
        return channel, session_id, session

    def _refuse(self, channel: BookmarkKey | None, kind: StatusKind) -> None:
        """Tell a refused tune-in on its channel; a keyless one tells no one (D28)."""
        if channel is not None:
            self._feed.refused(channel, kind)

    def _tell_failure(self, session_id: str, session: StreamSession, kind: StatusKind) -> None:
        """Tell why an admitted session failed to open; the feed tells ``kind`` only if the
        session still owns its channel (a newer stream, or a close that was already told,
        means it doesn't). Either way this ``ended`` call also forgets the session as a
        still-playing stream (D78c), which matters for pruning an idle channel and for which
        stream the next hand-back reaches."""
        if session.bookmark_key is not None:
            self._feed.ended(session.bookmark_key, session_id, kind)

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
        session = StreamSession(
            load_day=_day_loader(self._ports.repos, station_id),
            opened_at=self._elapsed(),
            bookmark_key=_channel(request, station_id),
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
            self._resume_to(saved),
            self._config.timing,
        )
        try:
            tuned = await asyncio.to_thread(_place, schedule, saved, self._forgetter(key, saved))
            session.landing, session.clock_offset = tuned.landing, tuned.clock_offset
        finally:
            session.placed.set()

    def _resume_to(self, saved: SavedBookmark | None) -> datetime | None:
        """The bookmark's ``left_at`` plus the steady time away (D11, D47), the moment a
        resume walks to; ``None`` with no bookmark. A bookmark with no elapsed-clock
        reading is only ever hand-built (the service always records one): it gets
        ``None`` too, and the placement walks it to the wall clock."""
        if saved is None or saved.left_elapsed is None:
            return None
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

    # ---- the listener feed ------------------------------------------------------------------

    async def listener_events(self, request: EventsRequest) -> ListenerEvents:
        """One page's subscription to its channel (D74), up to twice the listener cap app-wide
        (D78b); it reads the station and the limit, never the schedule.

        Raises ``StationNotFoundError``, ``InvalidStreamSettingError`` (D27, logged) or
        ``SubscriptionLimitError``."""
        station_id, raw_limit = await asyncio.to_thread(
            _station_and_limit, self._ports.repos, request.call_letters
        )
        if station_id is None:
            raise StationNotFoundError(f"no station has the call letters {request.call_letters!r}")
        limit = self._listener_limit(raw_limit)
        channel = BookmarkKey(request.listener_key, station_id, request.year)
        return self._feed.subscribe(channel, _SUBSCRIPTIONS_PER_SLOT * limit)

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
            return self._assign(call, self._authorised(call), landing)
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
        following = await self._with_stored_cues(following)  # D85; never seq 0 (D86c)
        return self._assign(call, self._authorised(call), following)

    async def _with_stored_cues(self, assigned: Assigned) -> Assigned:
        """D85: the song as stored now, or as read at tune-in when the read fails. Stored
        cues that would leave it unplayable are ignored (D86b, from D9): the walk chose it
        as playable. Only the assignment changes; the day memo stays as read."""
        file = assigned.item.file
        if file is None:
            return assigned
        answer = await self._rereads.stored_cues(file.file_id)
        if isinstance(answer, Unread):
            return assigned
        fresh = file.with_stored_cues(answer, self._config.timing)
        if fresh == file:
            if answer != file.cues:
                logger.debug("stream_cue_reread_ignored", file_id=str(file.file_id))
            return assigned
        return replace(assigned, item=replace(assigned.item, file=fresh))

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

    def _assign(self, call: ItemCall, session: StreamSession, assigned: Assigned) -> ItemPayload:
        """Store ``call.seq``'s assignment and answer its payload; a new one is noticed once."""
        served = self._served(session, call.seq)  # a concurrent request may have assigned it
        if served is not None:
            return served
        payload = _payload(call.seq, assigned, self._config.timing)
        session.assigned[call.seq] = assigned
        self._notice(call, assigned)
        return payload

    def _notice(self, call: ItemCall, assigned: Assigned) -> None:
        """Log an item sent without its cues: none stored (D78), or a landing's dropped (D80)."""
        file = assigned.item.file
        if file is None:
            return  # unreachable for an assigned item; keeps mypy honest
        timing = self._config.timing
        if file.cues is None:
            self._notice_no_cues(call, assigned.item, file)
        elif file.cues_to_play(assigned.offset_ms, timing) is None:
            logger.warning(
                "stream_landing_tail_short",
                session_id=call.session_id,
                seq=call.seq,
                event_id=str(assigned.item.event_id),
                file_id=str(file.file_id),
                offset_ms=assigned.offset_ms,
                span_ms=file.span_ms(),
                min_span_ms=file.min_span_ms(timing),
            )

    def _notice_no_cues(self, call: ItemCall, item: ScheduleItem, file: PlayableFile) -> None:
        """Warn about and report a file sent without cues once per app run, then debug (D82)."""
        fields = {
            "session_id": call.session_id,
            "seq": call.seq,
            "event_id": str(item.event_id),
            "file_id": str(file.file_id),
            "path": file.path,
        }
        if self._no_cues.seen(file.file_id):
            logger.debug("stream_item_no_cues", **fields)
            return
        logger.warning("stream_item_no_cues", **fields)
        self._no_cues.remember(file.file_id)
        self._ports.cue_reports.report(file.file_id)

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
            elapsed_now = self._elapsed()
            session.committed = Committed(call.seq, assigned, elapsed_now)
            if isinstance(assigned, Assigned):  # the final clip has no title
                shows_at = elapsed_now + self._config.now_playing_delay
                self._show_title(call.session_id, session, assigned.item, shows_at)
        self.stop_frozen()  # spec: the watchdog runs "on each started"

    def _show_title(
        self, session_id: str, session: StreamSession, item: ScheduleItem, shows_at: datetime
    ) -> None:
        """Time the item's title to show at ``shows_at``, on ICY (D25) and on the channel."""
        session.now_playing = (shows_at, f"{item.artist} - {item.title}")
        if session.bookmark_key is not None:
            told = NowPlaying(artist=item.artist, title=item.title)
            self._feed.title(session.bookmark_key, session_id, told, shows_at)

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
        """Free the slot and tell the channel ``ended`` (signed off) or ``stopped`` (D78a)
        before stopping the engine, so a stop that raises (D78c review M1) still frees the
        feed's hold on this session; the engine is stopped without waiting, and the bookmark
        is kept or cleared, afterwards. Telling the channel always comes before the bookmark
        write, so a reconnect can never read a stale one."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        key = session.bookmark_key
        finished = False
        if key is not None:
            finished = _schedule_finished(session)
            self._feed.ended(key, session_id, StatusKind.ENDED if finished else StatusKind.STOPPED)
        self._stop(session)
        if key is None:
            return  # D28
        if finished:
            self._bookmarks.delete(key)  # D26
            return
        now = self._ports.clock()
        saved = _left_at(session, wall_now=now, elapsed_now=self._elapsed())
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

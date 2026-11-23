"""The cache fill's PostgreSQL rig (MusicBrainz cache fill, phase 4.1 PR 2): seeds, a MusicBrainz
stand-in, and runs of the fill's tasks.

The fill runs for real: its tasks, its SQL and the real ``MusicBrainzApiClient``, writing the test
database's ``mb_cache``. Only the network, the clock and Huey's queue are stand-ins:

- ``MbServer`` is the client's mock transport. It answers every search with nothing found unless
  a test says otherwise, and records each request as the call it makes (the fill asks each call
  once, a 503 included: phase 4.1 PR 2b, D10). Each request steps a fake monotonic clock by
  ``seconds_per_call``, and notes what another connection sees as it arrives: the fill's RUNNING
  row and the ``mb_cache`` rows already committed. The fill's pause after a refusal (D10) steps
  the same clock instead of sleeping, and is noted in ``pauses``.
- What a task queues lands in ``Fill.queue`` (``huey.enqueue``), and ``huey.pending()`` reads it,
  so the fill's guard sees a queued batch as Huey would show it.

The patch points are the fill task module's own names: ``MusicBrainzApiClient`` (built with the
cache repository first), ``monotonic`` (the batch's clock), ``sleep`` (the pause) and
``FILL_LIMITS``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from tenacity import wait_none

from backend.config import get_settings
from backend.domain.enums import FillTier
from backend.services.cache_fill_constants import FILL_LIMITS, FillLimits
from backend.services.mb_client import MusicBrainzApiClient
from backend.services.normalization import normalize_artist, normalize_title
from backend.tasks import cache_fill_tasks, huey_app

type Conn = psycopg.Connection[DictRow]

DAY = datetime(2001, 1, 1, 8, tzinfo=UTC)
BATCH_TASK = "mb_cache_fill_batch_task"
RECHECK_TASK = "rematch_names_task"


def _raise(exc: Callable[[httpx.Request], BaseException]) -> Callable[[httpx.Request], Any]:
    def answer(request: httpx.Request) -> httpx.Response:
        raise exc(request)

    return answer


# What a request can get instead of an answer: an exception the transport raises, or a status.
# "crash" stands in for the worker dying mid-batch (E4): nothing on the way catches it.
OUTCOMES: dict[str, Callable[[httpx.Request], httpx.Response]] = {
    "timeout": _raise(lambda r: httpx.ReadTimeout("timed out", request=r)),
    "connect": _raise(lambda r: httpx.ConnectError("connection refused", request=r)),
    "503": lambda r: httpx.Response(503, request=r),
    "400": lambda r: httpx.Response(400, json={"error": "Invalid query."}, request=r),
    "crash": _raise(lambda _r: RuntimeError("the worker died")),
}


# --- the MusicBrainz stand-in -------------------------------------------------------------------


@dataclass(frozen=True)
class Asked:
    """One request, read back from its query.

    ``kind`` is ``"name_title"`` (a name + title search, ``words`` empty for a title-only one),
    ``"recording"`` (C18's search under one artist MBID) or ``"aliases"`` (an ``arid:`` artist
    search). Words and titles are as sent, Lucene escaping undone.
    """

    kind: str
    words: tuple[str, ...] = ()
    title: str = ""
    mbid: str = ""
    mbids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Seen:
    """What another connection saw as a request arrived."""

    progress: dict[str, Any] | None  # the fill's RUNNING row's progress_data, if any
    cached: int  # mb_cache rows committed


_ESCAPED = re.compile(r"\\(.)")
_PHRASE = re.compile(r'recording:"((?:\\.|[^"\\])*)"$')
_NAMED = re.compile(r"^artist:\((.*)\) AND recording:")
_ARID_ONE = re.compile(r"^arid:(\S+) AND recording:")
_ARID_MANY = re.compile(r"^arid:\((.*)\)$")


def _unescape(text: str) -> str:
    return _ESCAPED.sub(r"\1", text)


def _word(term: str) -> str:
    if len(term) >= 2 and term.startswith('"') and term.endswith('"'):
        term = term[1:-1]
    return _unescape(term)


def read_request(request: httpx.Request) -> Asked:
    """The call a request makes, from its path and its Lucene query."""
    query = parse_qs(request.url.query.decode())["query"][0]
    if request.url.path.rstrip("/").endswith("/artist"):
        many = _ARID_MANY.match(query)
        assert many is not None, query
        return Asked("aliases", mbids=frozenset(m.strip() for m in many[1].split(" OR ")))
    phrase = _PHRASE.search(query)
    assert phrase is not None, query
    title = _unescape(phrase[1])
    one = _ARID_ONE.match(query)
    if one is not None:
        return Asked("recording", title=title, mbid=_unescape(one[1]))
    named = _NAMED.match(query)
    words = tuple(_word(t) for t in named[1].split(" AND ")) if named is not None else ()
    return Asked("name_title", words=words, title=title)


@dataclass
class MbServer:
    """MusicBrainz, as the client's transport sees it."""

    observer: Conn
    clock: float = 0.0
    seconds_per_call: float = 1.0
    # Per request, in order, before ``fail`` and ``down``: an outcome name, or None to answer.
    script: list[str | None] = field(default_factory=list)
    fail: dict[str, str] = field(default_factory=dict)  # by title or alias MBID: an outcome
    down: str | None = None  # every request's outcome when set
    found: dict[str, dict[str, Any]] = field(default_factory=dict)  # name-title answers, by title
    aliases: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # by artist MBID
    unknown: set[str] = field(default_factory=set)  # MBIDs the search index doesn't know
    requests: list[Asked] = field(default_factory=list)
    seen: list[Seen] = field(default_factory=list)
    pauses: list[tuple[int, float]] = field(default_factory=list)  # requests before, seconds

    def __call__(self, request: httpx.Request) -> httpx.Response:
        asked = read_request(request)
        self.requests.append(asked)
        self.seen.append(self._look())
        self.clock += self.seconds_per_call
        outcome = self._outcome(asked)
        if outcome is not None:
            return OUTCOMES[outcome](request)
        return httpx.Response(200, json=self._answer(asked), request=request)

    def pause(self, seconds: float) -> None:
        """The fill's pause after a refusal (D10): the clock steps on instead of sleeping."""
        self.pauses.append((len(self.requests), seconds))
        self.clock += seconds

    def _outcome(self, asked: Asked) -> str | None:
        if self.script:
            return self.script.pop(0)
        keys = [asked.title, *sorted(asked.mbids)]
        for key in keys:
            if key and key in self.fail:
                return self.fail[key]
        return self.down

    def _answer(self, asked: Asked) -> dict[str, Any]:
        if asked.kind == "aliases":
            return {
                "artists": [
                    {"id": mbid, "aliases": self.aliases.get(mbid, [])}
                    for mbid in sorted(asked.mbids)
                    if mbid not in self.unknown
                ]
            }
        if asked.kind == "name_title":
            return self.found.get(asked.title, {"count": 0, "recordings": []})
        return {"count": 0, "recordings": []}

    def _look(self) -> Seen:
        row = self.observer.execute(
            """SELECT progress_data FROM progress_tracking
               WHERE task_type = 'mb_cache_fill' AND status = 'running'
               ORDER BY updated_at DESC LIMIT 1"""
        ).fetchone()
        count = self.observer.execute("SELECT count(*) AS n FROM mb_cache").fetchone()
        assert count is not None
        return Seen(progress=None if row is None else dict(row["progress_data"]), cached=count["n"])

    def asked(self, kind: str | None = None) -> list[Asked]:
        """The requests made, of *kind* if given."""
        return [a for a in self.requests if kind is None or a.kind == kind]

    def titles(self) -> list[str]:
        """The titles searched (name + title and C18), in order."""
        return [a.title for a in self.requests if a.kind != "aliases"]


# --- the fill, run on the test database ---------------------------------------------------------


def limits(**changes: Any) -> FillLimits:
    """FILL_LIMITS with *changes*; ``caps`` given are merged into its caps."""
    caps = {**FILL_LIMITS.caps, **changes.pop("caps", {})}
    return replace(FILL_LIMITS, caps=caps, **changes)


class Fill:
    """The fill's tasks on the test database, with ``MbServer`` for MusicBrainz."""

    def __init__(self, conn: Conn, url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self._conn = conn
        self._observer: Conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
        self.server = MbServer(self._observer)
        self.queue: list[Any] = []  # queued and not yet run
        self.handed: list[Any] = []  # everything ever queued, in order
        monkeypatch.setenv("DATABASE_URL", url)
        get_settings.cache_clear()
        monkeypatch.setattr(huey_app.huey, "enqueue", self._enqueue)
        monkeypatch.setattr(huey_app.huey, "pending", lambda *_a, **_k: list(self.queue))
        # A 503 is retried by the client; don't wait out its backoff.
        retry = MusicBrainzApiClient._fetch.retry  # type: ignore[attr-defined]
        monkeypatch.setattr(retry, "wait", wait_none())
        monkeypatch.setattr(cache_fill_tasks, "MusicBrainzApiClient", self._client)
        monkeypatch.setattr(cache_fill_tasks, "monotonic", lambda: self.server.clock)
        monkeypatch.setattr(cache_fill_tasks, "sleep", self.server.pause)
        self._monkeypatch = monkeypatch

    def close(self) -> None:
        self._observer.close()
        get_settings.cache_clear()

    def _enqueue(self, task: Any) -> None:
        self.queue.append(task)
        self.handed.append(task)

    def _client(self, cache_repo: Any, *args: Any, **kwargs: Any) -> MusicBrainzApiClient:
        ttl_days = kwargs.get("ttl_days", args[0] if args else 30)
        return MusicBrainzApiClient(
            cache_repo,
            ttl_days=ttl_days,
            transport=httpx.MockTransport(self.server),
            wait_turn=lambda: None,
        )

    def use(self, fill_limits: FillLimits) -> None:
        """Run with *fill_limits* instead of FILL_LIMITS."""
        self._monkeypatch.setattr(cache_fill_tasks, "FILL_LIMITS", fill_limits)

    def start(self) -> None:
        """Queue a run by name, as the runbook does: the start task, run at once."""
        self._conn.commit()
        cache_fill_tasks.mb_cache_fill_task.call_local()
        self._conn.rollback()

    def batch(self) -> bool:
        """Run the oldest queued batch, if any; True when one ran."""
        for task in self.queue:
            if task.name == BATCH_TASK:
                self.queue.remove(task)
                self._conn.commit()
                try:
                    cache_fill_tasks.mb_cache_fill_batch_task.call_local(*task.args, **task.kwargs)
                finally:
                    self._conn.rollback()
                return True
        return False

    def run(self, max_batches: int = 50) -> int:
        """Start a run and work it to its end; the number of batches it took."""
        self.start()
        batches = 0
        while self.batch():
            batches += 1
            assert batches <= max_batches, "the run never ended"
        return batches

    def batches_queued(self) -> list[Any]:
        """Every batch ever queued, in order."""
        return [t for t in self.handed if t.name == BATCH_TASK]

    def batches_waiting(self) -> list[Any]:
        """The batches queued and not yet run (re-checks queued stay in ``queue`` too)."""
        return [t for t in self.queue if t.name == BATCH_TASK]

    def rechecks(self) -> list[list[str]]:
        """The names of each re-check the fill queued, in order, each list sorted."""
        return [sorted(t.args[0]) for t in self.handed if t.name == RECHECK_TASK]

    def rows(self) -> list[dict[str, Any]]:
        """The fill's progress rows, oldest first: status and progress_data."""
        return [
            {"status": r["status"], **dict(r["progress_data"])}
            for r in self._observer.execute(
                """SELECT status, progress_data FROM progress_tracking
                   WHERE task_type = 'mb_cache_fill' ORDER BY started_at, updated_at"""
            ).fetchall()
        ]

    def last(self) -> dict[str, Any]:
        rows = self.rows()
        assert rows, "the fill wrote no progress row"
        return rows[-1]


def nonzero(counts: dict[str, int]) -> dict[str, int]:
    """*counts* without its zeros: a tier that made no call may be listed or not."""
    return {k: v for k, v in counts.items() if v}


# --- seeds ----------------------------------------------------------------------------------


def _one(row: DictRow | None, column: str = "id") -> Any:
    assert row is not None
    return row[column]


@dataclass(frozen=True)
class Name:
    """A log name: its id and its normalized name (what a re-check is given)."""

    id: Any
    normalized: str


def log_name(conn: Conn, original: str, status: str = "needs_review") -> Name:
    """The log name *original* in *status*, normalized as the stage normalizes it."""
    normalized = normalize_artist(original)
    row = conn.execute(
        """INSERT INTO broadcast_artists (original_name, normalized_name, match_status)
           VALUES (%s, %s, %s) RETURNING id""",
        (original, normalized, status),
    ).fetchone()
    return Name(_one(row), normalized)


def station(conn: Conn) -> Any:
    return _one(
        conn.execute(
            """INSERT INTO stations (call_letters, format_name) VALUES ('KFIL', 'AOR')
               ON CONFLICT (call_letters) DO UPDATE SET call_letters = EXCLUDED.call_letters
               RETURNING id"""
        ).fetchone()
    )


def line(conn: Conn, name: Name, title: str, plays: int = 1, status: str = "needs_review") -> Any:
    """A log line titled *title* under *name*, played *plays* times."""
    identity = _one(
        conn.execute(
            """INSERT INTO track_identities
                   (broadcast_artist_id, original_title, normalized_title, normalized_signature,
                    match_status)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (name.id, title, normalize_title(title), uuid4().hex, status),
        ).fetchone()
    )
    if plays:
        playlist = _one(
            conn.execute(
                """INSERT INTO playlists (name, content_hash, station_id)
                   VALUES (%s, %s, %s) RETURNING id""",
                (f"log-{uuid4()}.csv", uuid4().hex, station(conn)),
            ).fetchone()
        )
        conn.execute(
            """INSERT INTO play_events (identity_id, playlist_id, played_at)
               SELECT %s, %s, %s + n * interval '1 minute' FROM generate_series(1, %s) n""",
            (identity, playlist, DAY, plays),
        )
    return identity


def library_file(
    conn: Conn,
    title: str,
    *,
    artist: str = "Somebody",
    mbids: list[str] | None = None,
    status: str = "present",
) -> Any:
    """A library file titled *title* by *artist*, credited to *mbids* (untagged when None)."""
    return _one(
        conn.execute(
            """INSERT INTO library_files
                   (file_path, format, file_status, missing_since, track_title, normalized_title,
                    artist_name, normalized_artist_name, artist_mbid, artist_mbids)
               VALUES (%s, 'flac', %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                f"/music/{uuid4().hex}.flac",
                status,
                DAY if status == "missing" else None,
                title,
                normalize_title(title),
                artist,
                normalize_artist(artist),
                mbids[0] if mbids else None,
                mbids,
            ),
        ).fetchone()
    )


def catalog_artist(conn: Conn, name: str, mbid: str | None = None) -> str:
    """A catalog artist named *name* holding *mbid* (none when None)."""
    artist_id = f"artist-{uuid4()}"
    conn.execute(
        """INSERT INTO artists (id, name, sort_name, mbid, normalized_name)
           VALUES (%s, %s, %s, %s, %s)""",
        (artist_id, name, name, mbid, normalize_artist(name)),
    )
    return artist_id


def known_name(conn: Conn, artist_id: str, name: str, source: str = "tag") -> None:
    """One more name of the catalog artist (``artist_names``)."""
    conn.execute(
        """INSERT INTO artist_names (artist_id, name, normalized_name, source)
           VALUES (%s, %s, %s, %s)""",
        (artist_id, name, normalize_artist(name), source),
    )


def link(
    conn: Conn, name: Name, artist_id: str, *, state: str = "active", origin: str = "automatic"
) -> None:
    """A link from the log name to the catalog artist."""
    conn.execute(
        """INSERT INTO log_name_links
               (broadcast_artist_id, artist_id, mbid, state, origin, strength, evidence)
           SELECT %s, a.id, a.mbid, %s, %s, 100, %s FROM artists a WHERE a.id = %s""",
        (name.id, state, origin, Jsonb({"version": 1}), artist_id),
    )


def confirmation(conn: Conn, name: Name, title: str, queued_at: datetime) -> None:
    """A (name, title) pair queued for the fill (0058's table), as a later step queues one."""
    conn.execute(
        """INSERT INTO mb_cache_fill_confirmations
               (broadcast_artist_id, normalized_title, title, queued_at)
           VALUES (%s, %s, %s, %s)""",
        (name.id, normalize_title(title), title, queued_at),
    )


EMPTY_ANSWER: dict[str, Any] = {"count": 0, "title_only": False, "recordings": []}


def cached(
    conn: Conn, key: str, *, expired: bool = False, data: dict[str, Any] | None = None
) -> None:
    """A cache row under *key*, live for a month, or expired a day ago. Expiry is read against
    the database's clock, so these two stamps are the only ones taken from real time."""
    now = datetime.now(UTC)
    conn.execute(
        """INSERT INTO mb_cache (cache_key, entity_type, entity_mbid, response_data, cached_at,
                                 expires_at)
           VALUES (%s, %s, '', %s, %s, %s)""",
        (
            key,
            key.split(":", 1)[0],
            Jsonb(data if data is not None else EMPTY_ANSWER),
            now - timedelta(days=31 if expired else 1),
            now - timedelta(days=1) if expired else now + timedelta(days=30),
        ),
    )


def cache_keys(conn: Conn, prefix: str = "") -> set[str]:
    rows = conn.execute(
        "SELECT cache_key FROM mb_cache WHERE starts_with(cache_key, %s)", (prefix,)
    ).fetchall()
    return {r["cache_key"] for r in rows}


def cache_entry(conn: Conn, key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT response_data FROM mb_cache WHERE cache_key = %s", (key,)).fetchone()
    return None if row is None else dict(row["response_data"])


def status_of(conn: Conn, name: Name) -> str:
    row = conn.execute(
        "SELECT match_status FROM broadcast_artists WHERE id = %s", (name.id,)
    ).fetchone()
    return str(_one(row, "match_status"))


def key(words: str, title: str) -> str:
    """A name + title search's cache key (PR 1's): the words and the title, lower-cased."""
    return f"recording-name-title:{words.lower()}:{title.lower()}"


TIERS = tuple(FillTier)

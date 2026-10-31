"""Phase 0b gate (SPEC 2026-10-28): the matching dry runs (S3, S4, S5, S6, E2, E3).

``scripts/dry_run_matching.py --dsn <db> --stage artist|identity --out <file>`` runs a matching
stage's engines on every log name (artist) or on every line of a resolved name (identity),
through the service functions the tasks call, and writes what each would decide. It never
writes: one REPEATABLE READ, READ ONLY transaction, rolled back, and MusicBrainz is only its
cache (``CacheOnlyMusicBrainzClient`` over ``ReadOnlyMusicBrainzCache``).

The file has the recorders' shape (``header``, ``sections``). The artist stage's sections are
``artist-stage`` (log name id -> ``name``, ``status``, ``tier``, ``target``: ``artist_id`` and
``mbid``, or null, ``score``, ``reason``) and ``artist-cache`` (``hits``, ``misses``); the
identity stage's are ``identity-stage`` (line id -> ``status``, ``tier``, ``file``, ``score``,
``reason``) and ``identity-cache``. ``file`` is the file the service would write as the line's
match, so none for an auto_rejected line. A name whose MusicBrainz call misses the cache has
status ``unavailable``. A cache miss counts as a live fetch the client never makes
(``live_fetches``, as the production client counts calls past its cache). ``run(dsn, stage,
hook=None)`` returns the same document; the hook is called with the stage's own connection after
each step of the run, the first after the run's first read (``hook(step, conn)``). ``--compare``
works as the snapshot's (S2).

Seeds go through the scan, the ingestion task, the routes and the matching tasks (``gate_world``).
The MusicBrainz cache is filled by the production client, under its own keys, over a
MockTransport.
"""

from __future__ import annotations

import dataclasses
import inspect
import sys
import typing
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import CodeType, FrameType
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row, tuple_row

from backend.domain.matching import MusicBrainzUnavailableError
from backend.domain.system import MusicBrainzCache
from backend.repositories.musicbrainz_cache import MusicBrainzCacheRepository
from backend.services import artist_matching_service, identity_matching_service, mb_client
from backend.services.recording_search import RecordingSearch
from tests.integration import stream_seed as seed
from tests.integration.missing_file_seed import full_scan
from tests.scripts.gate_world import (
    Conn,
    catalog_artist,
    file_named,
    ingest,
    line_id,
    load_tool,
    name_id,
    production_client,
    production_client_factory,
    read_json,
    refuse_network,
    resolve_name,
    run_artist_matching,
    run_identity_matching,
    scan,
    tagged,
    write_json,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("queue_in_memory")]

DAY = "1995-03-14"
MBID_BAND = "0b000000-0000-4000-8000-0000000000a1"
TRUNCATED_TARGET = "0b000000-0000-4000-8000-0000000000a2"
LINE_BAND = "0b000000-0000-4000-8000-0000000000a3"
RECORDING_B = "0b000000-0000-4000-8000-0000000000a4"
HIT_TARGET = "0b000000-0000-4000-8000-0000000000a5"
# 30 characters ending mid-word: what a fixed-width log field leaves (is_likely_truncated).
TRUNCATED = "TRUNCATED " + "X" * 20
HIT_NAME = "LONGHIT " + "Y" * 22
MISSED_NAME = "LONGMISS " + "Z" * 21
NAME_FIELDS = ("status", "tier", "target", "score", "reason")
LINE_FIELDS = ("status", "tier", "file", "score", "reason")


@pytest.fixture(scope="module")
def dry() -> Any:
    return load_tool("dry_run_matching")


# --- MusicBrainz's answers -----------------------------------------------------------------------


def artist_answer(entity: str, query: str) -> dict[str, Any]:
    """MusicBrainz's answer to the seeds' artist searches: one act each, scored 100, with names
    no fuzzy match would join to the log names."""
    acts = {TRUNCATED: ("Zebra Quartet", TRUNCATED_TARGET), HIT_NAME: ("Okapi Trio", HIT_TARGET)}
    assert entity == "artist" and query in acts, (entity, query)
    name, mbid = acts[query]
    return {"artists": [{"id": mbid, "name": name, "sort-name": name, "score": 100}]}


def recording_answer(entity: str, query: str) -> dict[str, Any]:
    """MusicBrainz's answer to Line Band's recording searches: only SONG B is known."""
    assert entity == "recording", (entity, query)
    if "song b" in query.lower():
        return {"recordings": [{"id": RECORDING_B, "title": "Song B"}]}
    return {"recordings": []}


# --- seeds ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Names:
    """Six log names at one AC station: EXACT BAND (a local act of that name), MBID BAND (a
    tagged act), NOBODY KNOWN (no act), TRUNCATED (a cut-off name only MusicBrainz knows),
    HAND PICKED (resolved by hand) and REJECTED ONE (rejected by hand)."""

    playlist: UUID
    exact: UUID
    mbid: UUID
    nobody: UUID
    truncated: UUID
    hand: UUID
    rejected: UUID
    exact_artist: str


def names_seed(conn: Conn, client: TestClient, tmp_path: Path) -> Names:
    root = tmp_path / "library"
    tagged(root / "exact.mp3", "Exact Band", "Exact Song")
    tagged(root / "mbid.mp3", "Mbid Band", "Mbid Song", artist_mbid=MBID_BAND)
    scan(conn, root)
    station = seed.station(conn, "AC")
    playlist = ingest(
        conn,
        station,
        [
            ("EXACT BAND", "EXACT SONG", f"{DAY} 08:00:00"),
            ("MBID BAND", "MBID SONG", f"{DAY} 08:04:00"),
            ("NOBODY KNOWN", "SOME SONG", f"{DAY} 08:08:00"),
            (TRUNCATED, "LONG SONG", f"{DAY} 08:12:00"),
            ("HAND PICKED", "HAND SONG", f"{DAY} 08:16:00"),
            ("REJECTED ONE", "REJECTED SONG", f"{DAY} 08:20:00"),
        ],
        "names",
    )
    exact_artist = catalog_artist(client, "Exact Band")["id"]
    hand, rejected = name_id(conn, "HAND PICKED"), name_id(conn, "REJECTED ONE")
    resolve_name(client, hand, exact_artist)
    response = client.post(
        f"/api/v1/matching/artists/{rejected}/resolve", json={"match_status": "manual_rejected"}
    )
    assert response.status_code == 200, response.text
    conn.commit()
    return Names(
        playlist=playlist,
        exact=name_id(conn, "EXACT BAND"),
        mbid=name_id(conn, "MBID BAND"),
        nobody=name_id(conn, "NOBODY KNOWN"),
        truncated=name_id(conn, TRUNCATED),
        hand=hand,
        rejected=rejected,
        exact_artist=exact_artist,
    )


@dataclass(frozen=True)
class Lines:
    """LINE BAND, resolved by hand to its MBID, and its five lines at a CHR station:

    - SONG A: a file tagged with the act's MBID (Tier 1, Step A);
    - SONG B: a file tagged only with a recording MBID MusicBrainz finds (Step B);
    - SONG C: an untagged file (Step C, by name);
    - SONG D: no file;
    - SONG R: a file of its own (a test may reject it by hand: ``reject_file``).

    OTHER ACT / LONE SONG is a line under a name nobody resolved.
    """

    playlist: UUID
    band: UUID
    song_a: UUID
    song_b: UUID
    song_c: UUID
    song_d: UUID
    song_r: UUID
    lone: UUID
    file_a: UUID
    file_b: UUID
    file_c: UUID
    file_r: UUID


def lines_seed(conn: Conn, client: TestClient, tmp_path: Path) -> Lines:
    root = tmp_path / "library"
    tagged(root / "song-a.mp3", "Line Band", "Song A", artist_mbid=LINE_BAND)
    tagged(root / "song-b.mp3", "Line Band", "Song B", recording_mbid=RECORDING_B)
    tagged(root / "song-c.mp3", "Line Band", "Song C")
    tagged(root / "song-r.mp3", "Line Band", "Song R")
    scan(conn, root)
    station = seed.station(conn, "CHR")
    playlist = ingest(
        conn,
        station,
        [("LINE BAND", f"SONG {x}", f"{DAY} 09:0{i}:00") for i, x in enumerate("ABCDR")]
        + [("OTHER ACT", "LONE SONG", f"{DAY} 09:30:00")],
        "lines",
    )
    band = name_id(conn, "LINE BAND")
    resolve_name(client, band, LINE_BAND)
    conn.commit()
    return Lines(
        playlist=playlist,
        band=band,
        song_a=line_id(conn, "LINE BAND", "SONG A"),
        song_b=line_id(conn, "LINE BAND", "SONG B"),
        song_c=line_id(conn, "LINE BAND", "SONG C"),
        song_d=line_id(conn, "LINE BAND", "SONG D"),
        song_r=line_id(conn, "LINE BAND", "SONG R"),
        lone=line_id(conn, "OTHER ACT", "LONE SONG"),
        file_a=file_named(conn, "song-a.mp3"),
        file_b=file_named(conn, "song-b.mp3"),
        file_c=file_named(conn, "song-c.mp3"),
        file_r=file_named(conn, "song-r.mp3"),
    )


def reject_file(client: TestClient, line: UUID, file_id: UUID) -> None:
    """Reject *file_id* for *line* by hand (Reject in the Resolution Center)."""
    response = client.post(
        f"/api/v1/matching/identities/{line}/resolve",
        json={"match_status": "manual_rejected", "library_file_id": str(file_id)},
    )
    assert response.status_code == 200, response.text


@dataclass(frozen=True)
class Calm:
    """CALM BAND, resolved by hand to its local act (no MBID: matching never asks MusicBrainz),
    with CALM ONE (a file) and CALM TWO (no file yet); ARTIST, a log name no act holds yet."""

    playlist: UUID
    root: Path
    calm_one: UUID
    calm_two: UUID
    artist: UUID


def calm_seed(conn: Conn, client: TestClient, tmp_path: Path) -> Calm:
    root = tmp_path / "library"
    tagged(root / "calm-one.mp3", "Calm Band", "Calm One")
    scan(conn, root)
    station = seed.station(conn, "AC")
    playlist = ingest(
        conn,
        station,
        [
            ("CALM BAND", "CALM ONE", f"{DAY} 10:00:00"),
            ("CALM BAND", "CALM TWO", f"{DAY} 10:04:00"),
            ("ARTIST", "ANY SONG", f"{DAY} 10:08:00"),
        ],
        "calm",
    )
    resolve_name(client, name_id(conn, "CALM BAND"), catalog_artist(client, "Calm Band")["id"])
    conn.commit()
    return Calm(
        playlist=playlist,
        root=root,
        calm_one=line_id(conn, "CALM BAND", "CALM ONE"),
        calm_two=line_id(conn, "CALM BAND", "CALM TWO"),
        artist=name_id(conn, "ARTIST"),
    )


# --- helpers -------------------------------------------------------------------------------------


@contextmanager
def network_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """A guard client: an HTTP request, or the production client's rate-limit wait, is recorded
    (and raises), so a test can assert that none happened even if a tool caught the error."""
    touched: list[str] = []

    def send(*_args: object, **_kwargs: object) -> httpx.Response:
        touched.append("send")
        raise AssertionError("the dry run touched the network")

    def wait() -> None:
        touched.append("rate limit")
        raise AssertionError("the dry run waited on the MusicBrainz rate limit")

    with monkeypatch.context() as patch:
        patch.setattr(httpx.Client, "send", send)
        patch.setattr(mb_client, "_rate_limit", wait)
        yield touched


def run_cli(dry: Any, dsn: str, stage: str, out: Path) -> dict[str, Any]:
    assert dry.main(["--dsn", dsn, "--stage", stage, "--out", str(out)]) == 0
    return read_json(out)


def pick(entry: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: entry[field] for field in fields}


def scalar(conn: psycopg.Connection[Any], query: str) -> Any:
    with conn.cursor(row_factory=tuple_row) as cur:
        found = cur.execute(query).fetchone()
    assert found is not None
    return found[0]


# --- S3, S4 --------------------------------------------------------------------------------------


def test_artist_stage_covers_every_name(
    dry: Any,
    conn: Conn,
    client: TestClient,
    bench: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_rate_limit: None,
) -> None:
    """S3: every log name gets the artist stage's decision, whatever its status; for the names
    the task then matches, it decides exactly that."""
    names = names_seed(conn, client, tmp_path)
    production_client(conn, artist_answer, ttl_days=30).search_artist(TRUNCATED)
    conn.commit()

    doc = run_cli(dry, bench, "artist", tmp_path / "artist.json")

    assert doc["header"]["stage"] == "artist"
    stage = doc["sections"]["artist-stage"]
    every = {str(r["id"]) for r in conn.execute("SELECT id FROM broadcast_artists").fetchall()}
    assert set(stage) == every
    assert stage[str(names.exact)]["name"] == "EXACT BAND"
    mbid_artist = catalog_artist(client, "Mbid Band")
    deferred = {
        "status": "needs_review",
        "tier": "unclassified",
        "target": None,
        "score": 0.0,
        "reason": "DEFERRED_RETRY",
    }
    assert pick(stage[str(names.exact)], NAME_FIELDS) == {
        "status": "auto_matched",
        "tier": "normalization",
        "target": {"artist_id": names.exact_artist, "mbid": None},
        "score": 100.0,
        "reason": None,
    }
    assert pick(stage[str(names.mbid)], NAME_FIELDS) == {
        "status": "auto_matched",
        "tier": "normalization",
        "target": {"artist_id": mbid_artist["id"], "mbid": mbid_artist["mbid"]},
        "score": 100.0,
        "reason": None,
    }
    assert pick(stage[str(names.nobody)], NAME_FIELDS) == deferred
    assert pick(stage[str(names.truncated)], NAME_FIELDS) == {
        "status": "auto_matched",
        "tier": "musicbrainz_api",
        "target": {"artist_id": None, "mbid": TRUNCATED_TARGET},
        "score": 100.0,
        "reason": None,
    }
    # the engines' decision, not the stored one: nothing local holds these names
    assert pick(stage[str(names.hand)], NAME_FIELDS) == deferred
    assert pick(stage[str(names.rejected)], NAME_FIELDS) == deferred
    assert doc["sections"]["artist-cache"] == {"hits": 1, "misses": 0}

    run_artist_matching(
        monkeypatch, names.playlist, production_client_factory(refuse_network, ttl_days=30)
    )
    for name in (names.exact, names.mbid, names.nobody, names.truncated):
        stored = conn.execute(
            "SELECT match_status, reason_code FROM broadcast_artists WHERE id = %s", (name,)
        ).fetchone()
        assert stored is not None
        assert (stored["match_status"], stored["reason_code"]) == (
            stage[str(name)]["status"],
            stage[str(name)]["reason"],
        )

    # The task put MusicBrainz's act in the catalog, with its MBID: a log name of exactly that
    # name now links to it locally, by MBID.
    ingest(conn, seed.station(conn, "AC"), [("ZEBRA QUARTET", "Z", f"{DAY} 13:00:00")], "zq")
    zebra = catalog_artist(client, "Zebra Quartet")
    assert zebra["mbid"] == TRUNCATED_TARGET
    again = run_cli(dry, bench, "artist", tmp_path / "again.json")["sections"]["artist-stage"]
    assert pick(again[str(name_id(conn, "ZEBRA QUARTET"))], NAME_FIELDS) == {
        "status": "auto_matched",
        "tier": "normalization",
        "target": {"artist_id": zebra["id"], "mbid": TRUNCATED_TARGET},
        "score": 100.0,
        "reason": None,
    }


def test_identity_stage_covers_every_line(
    dry: Any,
    conn: Conn,
    client: TestClient,
    bench: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_rate_limit: None,
) -> None:
    """S4: every line of a resolved name gets the identity stage's decision; the task, run on
    the same data, wrote exactly that. A line's rejected file is never its candidate."""
    lines = lines_seed(conn, client, tmp_path)
    run_identity_matching(
        monkeypatch, lines.playlist, production_client_factory(recording_answer, ttl_days=30)
    )
    reject_file(client, lines.song_r, lines.file_r)  # after the task: its search is cached

    doc = run_cli(dry, bench, "identity", tmp_path / "identity.json")

    assert doc["header"]["stage"] == "identity"
    stage = doc["sections"]["identity-stage"]
    decided = (lines.song_a, lines.song_b, lines.song_c, lines.song_d)
    assert set(stage) == {str(line) for line in (*decided, lines.song_r)}  # not LONE SONG
    for line in decided:
        stored = conn.execute(
            "SELECT match_status, match_tier, reason_code FROM track_identities WHERE id = %s",
            (line,),
        ).fetchone()
        match = conn.execute(
            "SELECT library_file_id, confidence_score FROM matches WHERE identity_id = %s",
            (line,),
        ).fetchone()
        assert stored is not None
        entry = stage[str(line)]
        assert (entry["status"], entry["tier"], entry["reason"]) == (
            stored["match_status"],
            stored["match_tier"],
            stored["reason_code"],
        )
        assert entry["file"] == (None if match is None else str(match["library_file_id"]))
        if match is not None:
            assert entry["score"] == pytest.approx(match["confidence_score"], rel=1e-5)
    assert pick(stage[str(lines.song_a)], ("status", "tier", "file")) == {
        "status": "auto_matched",
        "tier": "musicbrainz_id_exact",
        "file": str(lines.file_a),
    }
    assert pick(stage[str(lines.song_b)], ("status", "tier", "file")) == {
        "status": "auto_matched",
        "tier": "musicbrainz_id_search",
        "file": str(lines.file_b),
    }
    assert stage[str(lines.song_c)]["file"] == str(lines.file_c)
    assert stage[str(lines.song_r)]["file"] != str(lines.file_r)
    cache = doc["sections"]["identity-cache"]
    assert cache["misses"] == 0
    assert cache["hits"] > 0


# --- S5 ------------------------------------------------------------------------------------------


def test_read_only_cache_only_deterministic(
    dry: Any,
    conn: Conn,
    client: TestClient,
    bench: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_rate_limit: None,
) -> None:
    """S5: one REPEATABLE READ, READ ONLY transaction per stage, never committed; MusicBrainz is
    a cache-only subclass of the production client over a cache that refuses writes; two runs
    over unchanged data are byte-identical."""
    names = names_seed(conn, client, tmp_path)
    lines = lines_seed(conn, client, tmp_path)
    production_client(conn, artist_answer, ttl_days=30).search_artist(TRUNCATED)
    conn.commit()
    run_identity_matching(
        monkeypatch, lines.playlist, production_client_factory(recording_answer, ttl_days=30)
    )
    assert names.truncated

    for stage in ("artist", "identity"):
        first, second = tmp_path / f"{stage}-1.json", tmp_path / f"{stage}-2.json"
        run_cli(dry, bench, stage, first)
        run_cli(dry, bench, stage, second)
        assert first.read_bytes() == second.read_bytes()

    seen: dict[str, set[tuple[str, str, int]]] = {"artist": set(), "identity": set()}

    real_commit = psycopg.Connection.commit
    commits: list[str] = []

    def refuse_commit(self: psycopg.Connection[Any]) -> None:
        if self.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            commits.append(self.info.dbname)
            raise AssertionError("the dry run committed its transaction")
        real_commit(self)

    for stage in ("artist", "identity"):

        def hook(step: str, tool_conn: psycopg.Connection[Any], stage: str = stage) -> None:
            seen[stage].add(
                (
                    scalar(tool_conn, "SHOW transaction_isolation"),
                    scalar(tool_conn, "SHOW transaction_read_only"),
                    id(tool_conn),
                )
            )

        with monkeypatch.context() as patch:
            patch.setattr(psycopg.Connection, "commit", refuse_commit)
            dry.run(bench, stage, hook=hook)
        assert commits == []
        assert len(seen[stage]) == 1  # one connection, one transaction, the same settings
        [(isolation, read_only, _conn)] = seen[stage]
        assert (isolation, read_only) == ("repeatable read", "on")

    assert issubclass(dry.CacheOnlyMusicBrainzClient, mb_client.MusicBrainzApiClient)
    with psycopg.connect(bench, row_factory=dict_row) as raw:
        cache = dry.ReadOnlyMusicBrainzCache(raw)
        assert isinstance(cache, MusicBrainzCacheRepository)
        now = datetime.now(tz=UTC)
        entry = MusicBrainzCache(
            id=uuid4(),
            cache_key="artist-search:never written",
            entity_type="artist-search",
            entity_mbid="",
            response_data={"artists": []},
            cached_at=now,
            expires_at=now + timedelta(days=1),
        )
        writes: list[Callable[[], object]] = [
            lambda: cache.set(entry),
            lambda: cache.set_many([entry]),
            cache.delete_expired,
        ]
        for write in writes:
            with pytest.raises(dry.CacheWriteRefusedError):
                write()

        with network_guard(monkeypatch) as touched:
            only_cache = dry.CacheOnlyMusicBrainzClient(cache)
            assert only_cache.search_artist(TRUNCATED)[0]["id"] == TRUNCATED_TARGET
            with pytest.raises(MusicBrainzUnavailableError):
                only_cache.search_artist("A NAME NOBODY CACHED")
        assert touched == []  # no rate-limit wait, no request
        assert (only_cache.cache_hits, only_cache.live_fetches) == (1, 1)


def test_hit_uses_production_key_and_ignores_expiry(
    dry: Any,
    conn: Conn,
    client: TestClient,
    bench: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_rate_limit: None,
) -> None:
    """S5, E2: rows the tasks' own client wrote, under its own keys, are hits for the dry run
    even after they expired, and stay as they were."""
    names = names_seed(conn, client, tmp_path)
    run_artist_matching(
        monkeypatch, names.playlist, production_client_factory(artist_answer, ttl_days=-1)
    )
    lines = lines_seed(conn, client, tmp_path)
    run_identity_matching(
        monkeypatch, lines.playlist, production_client_factory(recording_answer, ttl_days=-1)
    )
    cached = {
        r["cache_key"]: r["expires_at"]
        for r in conn.execute("SELECT cache_key, expires_at FROM mb_cache").fetchall()
    }
    assert cached
    assert not conn.execute("SELECT 1 FROM mb_cache WHERE expires_at > now()").fetchall()
    conn.commit()

    with network_guard(monkeypatch) as touched:
        artist = run_cli(dry, bench, "artist", tmp_path / "artist.json")
        identity = run_cli(dry, bench, "identity", tmp_path / "identity.json")

    assert touched == []
    truncated = artist["sections"]["artist-stage"][str(names.truncated)]
    assert (truncated["status"], truncated["tier"], truncated["target"]["mbid"]) == (
        "auto_matched",
        "musicbrainz_api",
        TRUNCATED_TARGET,
    )
    assert artist["sections"]["artist-cache"] == {"hits": 1, "misses": 0}
    song_b = identity["sections"]["identity-stage"][str(lines.song_b)]
    assert pick(song_b, ("status", "tier", "file")) == {
        "status": "auto_matched",
        "tier": "musicbrainz_id_search",
        "file": str(lines.file_b),
    }
    assert identity["sections"]["identity-cache"]["misses"] == 0
    assert identity["sections"]["identity-cache"]["hits"] > 0
    after = {
        r["cache_key"]: r["expires_at"]
        for r in conn.execute("SELECT cache_key, expires_at FROM mb_cache").fetchall()
    }
    assert after == cached


# --- E2, E3 --------------------------------------------------------------------------------------


def test_one_miss_changes_nothing_else(
    dry: Any,
    conn: Conn,
    client: TestClient,
    bench: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_rate_limit: None,
) -> None:
    """E2: a cache miss is "unavailable" for that call only; the next name and line are
    unaffected, an expired entry is used, and the network is never touched (a guard client).
    Eight misses in a row cut nothing off (``RecordingSearch`` has no cut-off here), and one
    name's miss leaves the rest of the stage running (the artist search map is empty)."""
    names = names_seed(conn, client, tmp_path)
    station = seed.station(conn, "AC")
    ingest(
        conn,
        station,
        [(HIT_NAME, "HIT SONG", f"{DAY} 11:00:00"), (MISSED_NAME, "MISS SONG", f"{DAY} 11:04:00")],
        "e2-names",
    )
    production_client(conn, artist_answer, ttl_days=-1).search_artist(HIT_NAME)
    conn.commit()
    lines = lines_seed(conn, client, tmp_path)
    more = ingest(
        conn,
        seed.station(conn, "CHR"),
        [("LINE BAND", f"SONG {x}", f"{DAY} 12:0{i}:00") for i, x in enumerate("EFGHI")],
        "e2-lines",
    )
    for playlist in (lines.playlist, more):
        run_identity_matching(
            monkeypatch, playlist, production_client_factory(recording_answer, ttl_days=-1)
        )
    # The searches MusicBrainz answered with nothing are forgotten: SONG C to I now miss.
    conn.execute("DELETE FROM mb_cache WHERE response_data -> 'recordings' = '[]'::jsonb")
    conn.commit()

    searches: list[RecordingSearch] = []
    original_init = RecordingSearch.__init__

    def recording_init(self: RecordingSearch, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        searches.append(self)

    with network_guard(monkeypatch) as touched, monkeypatch.context() as patch:
        patch.setattr(RecordingSearch, "__init__", recording_init)
        artist = run_cli(dry, bench, "artist", tmp_path / "artist.json")
        identity = run_cli(dry, bench, "identity", tmp_path / "identity.json")

    assert touched == []
    by_name = artist["sections"]["artist-stage"]
    assert by_name[str(name_id(conn, MISSED_NAME))]["status"] == "unavailable"
    assert by_name[str(names.truncated)]["status"] == "unavailable"  # not cached in this test
    hit = by_name[str(name_id(conn, HIT_NAME))]
    assert (hit["status"], hit["tier"], hit["target"]["mbid"]) == (
        "auto_matched",
        "musicbrainz_api",
        HIT_TARGET,
    )
    assert by_name[str(names.exact)]["status"] == "auto_matched"
    assert artist["sections"]["artist-cache"] == {"hits": 1, "misses": 2}

    by_line = identity["sections"]["identity-stage"]
    assert pick(by_line[str(lines.song_b)], ("status", "tier", "file")) == {
        "status": "auto_matched",
        "tier": "musicbrainz_id_search",
        "file": str(lines.file_b),
    }
    assert by_line[str(lines.song_a)]["file"] == str(lines.file_a)
    missed = [lines.song_c, lines.song_d, lines.song_r]
    missed += [line_id(conn, "LINE BAND", f"SONG {x}") for x in "EFGHI"]
    for line in missed:
        assert pick(by_line[str(line)], ("status", "file", "reason")) == {
            "status": "needs_review",
            "file": None,
            "reason": "MB_UNAVAILABLE",
        }
    cache = identity["sections"]["identity-cache"]
    assert cache["hits"] >= 1
    assert cache["misses"] == len(missed) == 8
    assert searches
    assert not any(search.cut_off for search in searches)


def test_one_consistent_read(
    dry: Any,
    conn: Conn,
    client: TestClient,
    bench: str,
    tmp_path: Path,
) -> None:
    """E3: a write committed between two steps of a run, after its first read, is not seen by
    the rest of that run (nor by its connection): every section comes from one snapshot. The
    next run sees it."""
    calm = calm_seed(conn, client, tmp_path)

    def scans_once(probe: str) -> Callable[[str, Any], None]:
        """A hook that, the first time it is called, runs the scan on its own connection and
        commits, then checks the tool's connection cannot see what it wrote."""
        done: list[str] = []

        def hook(step: str, tool_conn: psycopg.Connection[Any]) -> None:
            if done:
                return
            done.append(step)
            with psycopg.connect(bench, row_factory=dict_row) as other:
                full_scan(other, calm.root)
                assert scalar(other, probe) == 1
            assert scalar(tool_conn, probe) == 0

        return hook

    # An act called "Artist" arrives mid-run, with ARTIST's song (the scan).
    tagged(calm.root / "artist.mp3", "Artist", "Any Song")
    probe = "SELECT count(*) FROM artists WHERE name = 'Artist'"
    first = dry.run(bench, "artist", hook=scans_once(probe))
    assert first["sections"]["artist-stage"][str(calm.artist)]["status"] == "needs_review"
    later = dry.run(bench, "artist")
    assert later["sections"]["artist-stage"][str(calm.artist)]["status"] == "auto_matched"

    # CALM TWO's file arrives mid-run (the scan).
    tagged(calm.root / "calm-two.mp3", "Calm Band", "Calm Two")
    probe = "SELECT count(*) FROM library_files WHERE track_title = 'Calm Two'"
    first = dry.run(bench, "identity", hook=scans_once(probe))
    calm_two_file = str(file_named(conn, "calm-two.mp3"))
    assert first["sections"]["identity-stage"][str(calm.calm_two)]["file"] != calm_two_file
    later = dry.run(bench, "identity")
    assert later["sections"]["identity-stage"][str(calm.calm_two)]["file"] == calm_two_file


def test_compare_checks_the_cache_counts(
    dry: Any, conn: Conn, client: TestClient, bench: str, tmp_path: Path
) -> None:
    """S5, E4, E5: two runs over unchanged data compare equal; the hit and miss counts are
    compared; a file of another format version or stage is refused; an empty database gives
    empty sections and no cache use."""
    for stage in ("artist", "identity"):
        empty = run_cli(dry, bench, stage, tmp_path / f"{stage}-empty.json")
        assert empty["sections"] == {
            f"{stage}-stage": {},
            f"{stage}-cache": {"hits": 0, "misses": 0},
        }

    calm_seed(conn, client, tmp_path)
    old, new = tmp_path / "old.json", tmp_path / "new.json"
    data = run_cli(dry, bench, "artist", old)
    run_cli(dry, bench, "artist", new)
    report = tmp_path / "report.json"
    assert dry.main(["--compare", str(old), str(new), "--report", str(report)]) == 0

    more_hits = data["sections"]["artist-cache"]["hits"] + 1
    data["sections"]["artist-cache"]["hits"] = more_hits
    moved = write_json(tmp_path / "moved.json", data)
    assert dry.main(["--compare", str(old), str(moved), "--report", str(report)]) == 1
    assert [(d["section"], d["key"]) for d in read_json(report)["failed"]] == [
        ("artist-cache", "hits")
    ]

    newer = write_json(
        tmp_path / "newer.json",
        {**data, "header": {**data["header"], "format_version": dry.FORMAT_VERSION + 1}},
    )
    assert dry.main(["--compare", str(old), str(newer), "--report", str(report)]) == 2
    identity = tmp_path / "identity.json"
    run_cli(dry, bench, "identity", identity)
    assert dry.main(["--compare", str(old), str(identity), "--report", str(report)]) == 2


# --- S6 ------------------------------------------------------------------------------------------

SERVICE_STEPS: dict[str, Any] = {
    "build_local_artist_engine": artist_matching_service,
    "build_mb_artist_engine": artist_matching_service,
    "resolve_one_name": artist_matching_service,
    "build_identity_engine": identity_matching_service,
    "resolve_one_line": identity_matching_service,
    "load_exclusions": identity_matching_service,
    "group_by_artist": identity_matching_service,
}
BUILDERS = ("build_local_artist_engine", "build_mb_artist_engine", "build_identity_engine")


@contextmanager
def code_called() -> Iterator[set[CodeType]]:
    """The code objects of every Python function called in this thread inside the block."""
    seen: set[CodeType] = set()
    previous = sys.getprofile()

    def profile(frame: FrameType, event: str, _arg: object) -> None:
        if event == "call":
            seen.add(frame.f_code)

    sys.setprofile(profile)
    try:
        yield seen
    finally:
        sys.setprofile(previous)


def test_uses_the_service_functions(
    dry: Any,
    conn: Conn,
    client: TestClient,
    bench: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S6: the per-name and per-line steps and the engine builders are public, the builders
    take one dataclass of inputs, and the dry run and the tasks both go through them."""
    codes = {name: getattr(module, name).__code__ for name, module in SERVICE_STEPS.items()}
    for name in BUILDERS:
        builder = getattr(SERVICE_STEPS[name], name)
        first = next(iter(inspect.signature(builder).parameters))
        assert dataclasses.is_dataclass(typing.get_type_hints(builder)[first]), name

    calm = calm_seed(conn, client, tmp_path)  # ARTIST: no act, so the MusicBrainz phase runs

    with code_called() as seen:
        dry.run(bench, "artist")
        dry.run(bench, "identity")
    assert {name for name, code in codes.items() if code in seen} == set(codes)

    with code_called() as seen:
        run_artist_matching(monkeypatch, calm.playlist, None)
        run_identity_matching(monkeypatch, calm.playlist, None)
    assert {name for name, code in codes.items() if code in seen} == set(codes)

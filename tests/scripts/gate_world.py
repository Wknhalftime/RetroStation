"""Seeds and harness for the phase 0b gate tests (SPEC 2026-10-28, gate tools).

Seeds follow 0a's rule, as the 0b SPEC's Context requires: the library comes from the scan, log
names, lines and plays from the ingestion task, decisions from the routes and the matching
tasks' entry points, and anything else from the locked shared seed ``stream_seed``. Nothing here
writes ``matches.artist_id``, ``works.artist_id`` or ``matches.work_id`` itself, or calls
``Match(artist_id=...)``, ``create_local`` or ``upsert_from_mb``, so later phases run these
tests unchanged.

MusicBrainz is never reached. A matching task gets either a client that fails on any call or
the production client over an ``httpx.MockTransport`` that answers from a dict; the second is
how the MusicBrainz cache gets rows under the production keys (S5).
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from types import ModuleType
from typing import Any, NoReturn
from uuid import UUID

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import ID3, TIT2, TPE1, TXXX
from psycopg import sql as pg_sql
from psycopg.rows import DictRow

from backend.config import get_settings
from tests.integration import stream_seed as seed
from tests.integration.missing_file_seed import full_scan, put
from tests.lifespan_client import lifespan_client

type Conn = psycopg.Connection[DictRow]

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


# --- the tools and their database ----------------------------------------------------------------


def load_tool(name: str) -> ModuleType:
    """A gate tool from ``scripts/``, imported as its own module (as the locked
    ``test_bench_enrich.py`` imports ``bench_enrich``)."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def bench_name(test_dbname: str) -> str:
    return f"{test_dbname}_bench"


def with_dbname(dsn: str, dbname: str) -> str:
    params = psycopg.conninfo.conninfo_to_dict(dsn)
    return psycopg.conninfo.make_conninfo(**{**params, "dbname": dbname})


def dbname_of(dsn: str) -> str:
    return str(psycopg.conninfo.conninfo_to_dict(dsn)["dbname"])


def empty_database(dsn: str) -> None:
    """Every table but ``schema_migrations`` truncated, and no gate copy marker on the database.

    The table list is read from the catalog, so tables a later phase adds are emptied too.
    """
    with psycopg.connect(dsn, autocommit=True) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                """SELECT tablename FROM pg_tables
                   WHERE schemaname = 'public' AND tablename <> 'schema_migrations'"""
            ).fetchall()
        ]
        conn.execute(
            pg_sql.SQL("TRUNCATE {} CASCADE").format(
                pg_sql.SQL(", ").join(pg_sql.Identifier(t) for t in tables)
            )
        )
        conn.execute(
            pg_sql.SQL("COMMENT ON DATABASE {} IS NULL").format(pg_sql.Identifier(dbname_of(dsn)))
        )


def decoy_dsn(dsn: str) -> str:
    """A database that does not exist and holds no "bench": what DATABASE_URL names while a
    tool runs, so a tool that reached for it fails at once (and touches nothing)."""
    return with_dbname(dsn, "retrostation_gate_decoy_missing")


def run_guarded(bench: str, script: str, *args: str) -> subprocess.CompletedProcess[str]:
    """``scripts/<script> args`` in a fresh interpreter under ``gate_guard.py`` (S10), with
    DATABASE_URL naming the decoy and no STREAM_ENABLED or PYTEST_XDIST_WORKER set."""
    unset = ("STREAM_ENABLED", "PYTEST_XDIST_WORKER", "RETROSTATION_SKIP_BOOT_MIGRATIONS")
    env = {k: v for k, v in os.environ.items() if k not in unset}
    env["DATABASE_URL"] = decoy_dsn(bench)
    guard = Path(__file__).parent / "gate_guard.py"
    return subprocess.run(
        [sys.executable, str(guard), bench, str(SCRIPTS / script), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )


def read_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def write_json(path: Path, data: Mapping[str, Any]) -> Path:
    path.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    return path


# --- the app --------------------------------------------------------------------------------------


@contextmanager
def app_client(dsn: str) -> Iterator[TestClient]:
    """The app on *dsn*, its lifespan run with no boot migrations and streaming off (the
    ``lifespan_client`` recipe); only the token is bypassed."""
    patch = pytest.MonkeyPatch()
    patch.setenv("DATABASE_URL", dsn)
    patch.setenv("RETROSTATION_SKIP_BOOT_MIGRATIONS", "1")
    get_settings.cache_clear()
    from backend.dependencies import get_current_token
    from backend.main import app

    async def _token() -> str:
        return "test-token"

    app.dependency_overrides[get_current_token] = _token
    try:
        with lifespan_client(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        patch.undo()
        get_settings.cache_clear()


# --- MusicBrainz ----------------------------------------------------------------------------------


class NoMusicBrainz:
    """A MusicBrainz client a seed must never call: any call fails the test."""

    live_fetches = 0
    cache_hits = 0

    def __getattr__(self, name: str) -> NoReturn:
        raise AssertionError(f"the seed reached MusicBrainz ({name})")


type Answer = Callable[[str, str], Mapping[str, Any]]
"""MusicBrainz's answer to a search: (entity, query) -> the JSON body. It raises (fails the
test) for a search the seed did not expect."""


def answering_transport(answer: Answer) -> httpx.MockTransport:
    """A transport for the production client that answers each search from *answer*: the
    request path's entity (``"artist"``, ``"recording"``) and its ``query`` parameter."""

    def handle(request: httpx.Request) -> httpx.Response:
        entity = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        return httpx.Response(200, json=dict(answer(entity, request.url.params.get("query", ""))))

    return httpx.MockTransport(handle)


def refuse_network(entity: str, query: str) -> NoReturn:
    """An answer for a run that must find everything in the cache."""
    raise AssertionError(f"MusicBrainz was asked: {entity} {query!r}")


def production_client_factory(answer: Answer, *, ttl_days: int) -> Callable[..., Any]:
    """What a task module's ``MusicBrainzApiClient`` is replaced with: the real client over the
    task's own cache repository, with *answer* as the network, writing cache rows under its own
    keys that live for *ttl_days* (negative: already expired)."""
    from backend.services import mb_client

    def make(cache_repo: Any, **_kwargs: Any) -> Any:
        return mb_client.MusicBrainzApiClient(
            cache_repo, ttl_days=ttl_days, transport=answering_transport(answer)
        )

    return make


def production_client(conn: Conn, answer: Answer, *, ttl_days: int) -> Any:
    """The real client on *conn*'s cache, for a seed that searches directly (the caller
    commits)."""
    from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository

    return production_client_factory(answer, ttl_days=ttl_days)(PgMusicBrainzCacheRepository(conn))


# --- library, log and decisions -------------------------------------------------------------------


def tagged(
    dest: Path,
    artist: str,
    title: str,
    *,
    release_type: str = "album",
    artist_mbid: str | None = None,
    recording_mbid: str | None = None,
) -> Path:
    """A copy of the fixture MP3 at *dest* carrying only these tags (the scan reads them)."""
    put("well_tagged.mp3", dest)
    tags = ID3(str(dest))
    tags.clear()
    tags.add(TPE1(encoding=3, text=[artist]))
    tags.add(TIT2(encoding=3, text=[title]))
    tags.add(TXXX(encoding=3, desc="MusicBrainz Release Type", text=[release_type]))
    if artist_mbid is not None:
        tags.add(TXXX(encoding=3, desc="MusicBrainz Artist Id", text=[artist_mbid]))
    if recording_mbid is not None:
        tags.add(TXXX(encoding=3, desc="MusicBrainz Track Id", text=[recording_mbid]))
    tags.save()
    return dest


def scan(conn: Conn, root: Path) -> None:
    """The full library scan of *root*, committed."""
    full_scan(conn, root)


def file_named(conn: Conn, name: str) -> UUID:
    """The one library row whose file is called *name* (compared by name, never by LIKE)."""
    rows = conn.execute("SELECT id, file_path FROM library_files").fetchall()
    [file_id] = [UUID(str(r["id"])) for r in rows if PureWindowsPath(r["file_path"]).name == name]
    return file_id


def work_of(conn: Conn, file_id: UUID) -> str:
    """The id of the work a file is on, to address the works routes and to state expectations."""
    row = conn.execute("SELECT work_id FROM library_files WHERE id = %s", (file_id,)).fetchone()
    assert row is not None and row["work_id"] is not None
    return str(row["work_id"])


def catalog_artist(client: TestClient, name: str) -> dict[str, Any]:
    """The Library's artist called *name*, read through the artist list route."""
    items = client.get("/api/v1/library/artists", params={"search": name}).json()["items"]
    [artist] = [a for a in items if a["name"] == name]
    return dict(artist)


def ingest(conn: Conn, station: UUID, rows: list[tuple[str, str, str]], label: str) -> UUID:
    """One log for *station* through the ingestion task: (artist, title, "YYYY-MM-DD HH:MM:SS")
    rows. Returns the playlist id. Its hand-off to matching lands in the in-memory queue."""
    from backend.tasks.ingestion_tasks import ingestion_task

    conn.commit()
    body = "Artist,Title,Played\n" + "".join(f"{a},{t},{p}\n" for a, t, p in rows)
    ingestion_task.call_local(body.encode("utf-8"), f"{label}.csv", str(station), label)
    row = conn.execute(
        "SELECT id FROM playlists WHERE station_id = %s ORDER BY ingested_at DESC LIMIT 1",
        (station,),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def name_id(conn: Conn, original_name: str) -> UUID:
    row = conn.execute(
        "SELECT id FROM broadcast_artists WHERE original_name = %s", (original_name,)
    ).fetchone()
    assert row is not None, original_name
    return UUID(str(row["id"]))


def line_id(conn: Conn, artist: str, title: str) -> UUID:
    row = conn.execute(
        """SELECT ti.id FROM track_identities ti
           JOIN broadcast_artists ba ON ba.id = ti.broadcast_artist_id
           WHERE ba.original_name = %s AND ti.original_title = %s""",
        (artist, title),
    ).fetchone()
    assert row is not None, (artist, title)
    return UUID(str(row["id"]))


def resolve_name(client: TestClient, name: UUID, target: str) -> None:
    """Resolve a log name by hand (Resolve in the Resolution Center) to *target*, a catalog id
    or an MBID. Resolving again to another target adds a second link (today's behaviour)."""
    response = client.post(
        f"/api/v1/matching/artists/{name}/resolve",
        json={"match_status": "manual_matched", "target_artist_id": target},
    )
    assert response.status_code == 200, response.text


def resolve_line(client: TestClient, line: UUID, file_id: UUID) -> None:
    response = client.post(
        f"/api/v1/matching/identities/{line}/resolve",
        json={"match_status": "manual_matched", "library_file_id": str(file_id)},
    )
    assert response.status_code == 200, response.text


def run_artist_matching(
    monkeypatch: pytest.MonkeyPatch, playlist: UUID, client_factory: Callable[..., Any] | None
) -> None:
    """The artist matching task's entry point on *playlist*; MusicBrainz is *client_factory*,
    or a client that fails on any call."""
    from backend.tasks import artist_matching_tasks

    factory = client_factory or (lambda *_a, **_k: nullcontext(NoMusicBrainz()))
    monkeypatch.setattr(artist_matching_tasks, "MusicBrainzApiClient", factory)
    artist_matching_tasks.artist_matching_task.call_local(str(playlist))


def run_identity_matching(
    monkeypatch: pytest.MonkeyPatch, playlist: UUID, client_factory: Callable[..., Any] | None
) -> None:
    """The identity matching task's entry point on *playlist*, as run_artist_matching."""
    from backend.tasks import identity_matching_tasks

    factory = client_factory or (lambda *_a, **_k: nullcontext(NoMusicBrainz()))
    monkeypatch.setattr(identity_matching_tasks, "MusicBrainzApiClient", factory)
    identity_matching_tasks.identity_matching_task.call_local(str(playlist))


# --- the shared world for the snapshot and golden tests -------------------------------------------

BETA_MBID = "0b000000-0000-4000-8000-0000000000b1"


@dataclass(frozen=True)
class World:
    """Three stations, three log names and their lines, a small library and one override.

    - AC (format AC), CHR (format CHR) and NOFMT (no format) all play ALPHA BAND / FIRST SONG.
      Its work has two copies: the album copy is the master (scanned first), the single copy
      is the AC override. So AC plays the single, CHR and NOFMT the album (E1).
    - AC also plays ALPHA BAND / SECOND SONG (its own work), BETA GROUP / BETA HIT (a file
      tagged with Beta Group's MBID) and GAMMA / UNKNOWN SONG (no act and no file: GAMMA is
      deferred and its line with it, so it is AC's one missing match).
    - DELTA is a log name resolved by hand twice, to Alpha Band and to Beta Group: two links.
    - LONELY / NEVER PLAYED is a pending line with no plays (E1).
    - gone.mp3 was scanned, then deleted and rescanned: a missing file.
    """

    ac: UUID
    chr: UUID
    nofmt: UUID
    first_song: UUID
    second_song: UUID
    beta_hit: UUID
    unknown_song: UUID
    unplayed: UUID
    alpha: UUID
    beta: UUID
    gamma: UUID
    delta: UUID
    album: UUID
    single: UUID
    second: UUID
    beta_file: UUID
    gone: UUID
    first_work: str
    alpha_artist: str
    beta_artist: str
    ac_playlist: UUID


def build_world(
    conn: Conn, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> World:
    root = tmp_path / "library"
    tagged(root / "first-album.mp3", "Alpha Band", "First Song", release_type="album")
    tagged(root / "gone.mp3", "Alpha Band", "Gone Song")
    scan(conn, root)
    tagged(root / "first-single.mp3", "Alpha Band", "First Song", release_type="single")
    tagged(root / "second.mp3", "Alpha Band", "Second Song")
    tagged(root / "beta-hit.mp3", "Beta Group", "Beta Hit", artist_mbid=BETA_MBID)
    gone = file_named(conn, "gone.mp3")
    (root / "gone.mp3").unlink()
    scan(conn, root)
    album, single = file_named(conn, "first-album.mp3"), file_named(conn, "first-single.mp3")
    first_work = work_of(conn, album)
    assert work_of(conn, single) == first_work

    ac, chr_, nofmt = seed.station(conn, "AC"), seed.station(conn, "CHR"), seed.station(conn, None)
    override = client.post(
        f"/api/v1/library/works/{first_work}/format-overrides",
        json={"format_name": "AC", "preferred_file_id": str(single)},
    )
    assert override.status_code == 201, override.text
    day = "1995-03-14"
    ac_playlist = ingest(
        conn,
        ac,
        [
            ("ALPHA BAND", "FIRST SONG", f"{day} 08:00:00"),
            ("ALPHA BAND", "SECOND SONG", f"{day} 08:04:00"),
            ("BETA GROUP", "BETA HIT", f"{day} 08:08:00"),
            ("GAMMA", "UNKNOWN SONG", f"{day} 08:12:00"),
        ],
        "world-ac",
    )
    chr_playlist = ingest(conn, chr_, [("ALPHA BAND", "FIRST SONG", f"{day} 09:00:00")], "w-chr")
    nofmt_playlist = ingest(
        conn, nofmt, [("ALPHA BAND", "FIRST SONG", f"{day} 10:00:00")], "world-nofmt"
    )
    for playlist in (ac_playlist, chr_playlist, nofmt_playlist):
        run_artist_matching(monkeypatch, playlist, None)
        run_identity_matching(monkeypatch, playlist, None)

    alpha_artist = catalog_artist(client, "Alpha Band")["id"]
    beta_artist = catalog_artist(client, "Beta Group")["id"]
    delta_station = seed.station(conn, "AC")
    ingest(conn, delta_station, [("DELTA", "DELTA SONG", f"{day} 11:00:00")], "world-delta")
    delta = name_id(conn, "DELTA")
    resolve_name(client, delta, alpha_artist)
    resolve_name(client, delta, BETA_MBID)
    unplayed = seed.identity(conn, artist="LONELY", title="NEVER PLAYED", status="pending")
    conn.commit()
    return World(
        ac=ac,
        chr=chr_,
        nofmt=nofmt,
        first_song=line_id(conn, "ALPHA BAND", "FIRST SONG"),
        second_song=line_id(conn, "ALPHA BAND", "SECOND SONG"),
        beta_hit=line_id(conn, "BETA GROUP", "BETA HIT"),
        unknown_song=line_id(conn, "GAMMA", "UNKNOWN SONG"),
        unplayed=unplayed,
        alpha=name_id(conn, "ALPHA BAND"),
        beta=name_id(conn, "BETA GROUP"),
        gamma=name_id(conn, "GAMMA"),
        delta=delta,
        album=album,
        single=single,
        second=file_named(conn, "second.mp3"),
        beta_file=file_named(conn, "beta-hit.mp3"),
        gone=gone,
        first_work=first_work,
        alpha_artist=alpha_artist,
        beta_artist=beta_artist,
        ac_playlist=ac_playlist,
    )

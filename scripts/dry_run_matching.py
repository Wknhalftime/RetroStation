"""Matching dry runs on a gate copy, and their compare (phase 0b SPEC 2026-10-28, S3-S5, E2, E3).

    python scripts/dry_run_matching.py --dsn <bench db> --stage artist|identity --out <file>
    python scripts/dry_run_matching.py --compare <old> <new> [--report <file>] [--allow <file>]
        [--mode <section>=<mode> ...]

Matching only touches pending rows, so a dry run calls the stage's engines on every log name
(artist) or on every line of a resolved name (identity), through the service functions the
tasks call (S6): ``build_local_artist_engine``, ``build_mb_artist_engine`` and
``resolve_one_name``; ``build_identity_engine``, ``load_exclusions``, ``group_by_artist`` and
``resolve_one_line``. Each entry is what the task would decide:

- ``artist-stage``: log name id -> name, status, tier, target (catalog ``artist_id`` and
  ``mbid``, or null), score and reason; ``unavailable`` when its MusicBrainz call misses the
  cache. ``artist-cache``: the client's hits and misses.
- ``identity-stage``: line id -> status, tier, the file the task would write as its match
  (none for an auto_rejected line), score and reason. ``identity-cache``: likewise.

It never writes: one REPEATABLE READ, READ ONLY transaction, rolled back (S5). MusicBrainz is
only its cache: ``CacheOnlyMusicBrainzClient`` is the production client (so the cache keys are
the production ones) whose network call raises MusicBrainzUnavailableError without the
rate-limit wait, over ``ReadOnlyMusicBrainzCache``, whose reads ignore ``expires_at`` and whose
writes raise. A miss is "unavailable" for that call only: ``RecordingSearch`` has no cut-off,
the artist search map is empty, and each name and line catches the error on its own.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_common  # noqa: E402

if __name__ == "__main__":
    gate_common.guard_from_argv(sys.argv[1:])  # S10: before the backend imports below

from backend.config import get_settings  # noqa: E402
from backend.db.repositories.artists import PgArtistRepository  # noqa: E402
from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository  # noqa: E402
from backend.db.repositories.broadcast_track_identities import (  # noqa: E402
    PgBroadcastTrackIdentityRepository,
)
from backend.db.repositories.library_files import PgLibraryFileRepository  # noqa: E402
from backend.db.repositories.log_name_links import PgLogNameLinkRepository  # noqa: E402
from backend.db.repositories.mapping_rules import PgMappingRuleRepository  # noqa: E402
from backend.db.repositories.matches import PgMatchRepository  # noqa: E402
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository  # noqa: E402
from backend.domain.enums import MatchStatus  # noqa: E402
from backend.domain.matching import MusicBrainzUnavailableError  # noqa: E402
from backend.domain.system import MusicBrainzCache  # noqa: E402
from backend.repositories.musicbrainz_cache import MusicBrainzCacheRepository  # noqa: E402
from backend.services.artist_matching_service import (  # noqa: E402
    ArtistEngineInputs,
    ArtistMatchingRepos,
    ArtistMatchResult,
    ArtistMatchThresholds,
    build_local_artist_engine,
    build_mb_artist_engine,
    resolve_one_name,
)
from backend.services.identity_matching_service import (  # noqa: E402
    IdentityEngineInputs,
    IdentityMatchingRepos,
    IdentityMatchResult,
    build_identity_engine,
    group_by_artist,
    load_exclusions,
    resolve_one_line,
)
from backend.services.mb_client import MusicBrainzApiClient  # noqa: E402
from backend.services.recording_search import RecordingSearch  # noqa: E402

TOOL = "dry_run_matching"
FORMAT_VERSION = 1
STAGES = ("artist", "identity")
COMPARE = gate_common.CompareSpec(tool=TOOL)

type Conn = psycopg.Connection[Any]
type Hook = Callable[[str, Conn], None]

_RESOLVED = (MatchStatus.AUTO_MATCHED.value, MatchStatus.MANUAL_MATCHED.value)

# Every line of a resolved name, with the columns the pending-songs query reads.
_LINES_SQL = """
SELECT ti.id, ti.broadcast_artist_id, ti.original_title, ti.normalized_title,
       ti.normalized_signature, ti.match_status, ti.match_tier, ti.created_at,
       ti.reason_code, ti.reason_detail, ti.rejected_file_ids
FROM track_identities ti
JOIN broadcast_artists ba ON ba.id = ti.broadcast_artist_id
WHERE ba.match_status = ANY(%(resolved)s)
ORDER BY ti.broadcast_artist_id, ti.id
"""


class CacheWriteRefusedError(RuntimeError):
    """A dry run tried to write the MusicBrainz cache."""


class ReadOnlyMusicBrainzCache(MusicBrainzCacheRepository):
    """The MusicBrainz cache as a dry run reads it: every row under its production key,
    expired or not (S5, E2). Every write raises CacheWriteRefusedError."""

    def __init__(self, conn: Conn) -> None:
        self._conn = conn
        self._rows = PgMusicBrainzCacheRepository(conn)

    def get(self, cache_key: str) -> MusicBrainzCache | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT * FROM mb_cache WHERE cache_key = %s", (cache_key,)
            ).fetchone()
        return self._rows._row_to_model(row) if row else None

    def get_many(self, cache_keys: Sequence[str]) -> dict[str, MusicBrainzCache]:
        if not cache_keys:
            return {}
        with self._conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                "SELECT * FROM mb_cache WHERE cache_key = ANY(%s)", (list(cache_keys),)
            ).fetchall()
        return {row["cache_key"]: self._rows._row_to_model(row) for row in rows}

    def set(self, cache: MusicBrainzCache) -> None:
        raise CacheWriteRefusedError(f"a dry run never writes the cache ({cache.cache_key})")

    def set_many(self, caches: Sequence[MusicBrainzCache]) -> None:
        raise CacheWriteRefusedError(f"a dry run never writes the cache ({len(caches)} rows)")

    def delete_expired(self) -> int:
        raise CacheWriteRefusedError("a dry run never deletes from the cache")


class CacheOnlyMusicBrainzClient(MusicBrainzApiClient):
    """The production client whose network call raises MusicBrainzUnavailableError, with no
    rate-limit wait: a cache hit is answered as in production, a miss counts as a live fetch
    (``live_fetches``) the client never makes."""

    def __init__(self, cache: MusicBrainzCacheRepository) -> None:
        super().__init__(cache)

    def _fetch(self, url: str, params: dict[str, str]) -> httpx.Response:
        raise MusicBrainzUnavailableError(f"not in the MusicBrainz cache: {url} {params}")


def _cache_counts(client: MusicBrainzApiClient) -> dict[str, int]:
    return {"hits": client.cache_hits, "misses": client.live_fetches}


# --- artist stage (S3) ---------------------------------------------------------------------------


def _artist_repos(conn: Conn) -> ArtistMatchingRepos:
    """The repositories artist_matching_task wires, on the dry run's connection."""
    return ArtistMatchingRepos(
        broadcast_artist_repo=PgBroadcastArtistRepository(conn),
        track_identity_repo=PgBroadcastTrackIdentityRepository(conn),
        artist_repo=PgArtistRepository(conn),
        match_repo=PgMatchRepository(conn),
        rules_repo=PgMappingRuleRepository(conn),
        link_repo=PgLogNameLinkRepository(conn),
    )


def _artist_thresholds() -> ArtistMatchThresholds:
    """The thresholds artist_matching_task builds from the settings."""
    settings = get_settings()
    return ArtistMatchThresholds(
        strong_match_threshold=settings.strong_match_threshold,
        mb_score_gap=settings.mb_score_gap,
        mb_auto_link_score=settings.mb_auto_link_score,
        min_presentation_score=settings.min_presentation_score,
        broadcast_name_max_len=settings.broadcast_name_max_len,
    )


def _catalog(conn: Conn) -> tuple[dict[str, Any], dict[str, Any]]:
    """The catalog's artists by id and by MBID: a result's target is either."""
    rows = conn.execute("SELECT id, mbid FROM artists").fetchall()
    by_id = {row["id"]: row for row in rows}
    by_mbid = {row["mbid"]: row for row in rows if row["mbid"]}
    return by_id, by_mbid


def _artist_entry(
    name: str, result: ArtistMatchResult, catalog: tuple[Mapping[str, Any], Mapping[str, Any]]
) -> dict[str, Any]:
    target: dict[str, Any] | None = None
    if result.target_id:
        by_id, by_mbid = catalog
        found = by_id.get(result.target_id) or by_mbid.get(result.target_id)
        target = (
            {"artist_id": found["id"], "mbid": found["mbid"]}
            if found
            else {"artist_id": None, "mbid": result.target_id}
        )
    return {
        "name": name,
        "status": result.status.value,
        "tier": result.tier.value,
        "target": target,
        "score": result.confidence_score,
        "reason": None if result.reason_code is None else result.reason_code.value,
    }


def _unavailable(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "unavailable",
        "tier": None,
        "target": None,
        "score": None,
        "reason": None,
    }


def artist_stage(conn: Conn, client: MusicBrainzApiClient, hook: Hook) -> dict[str, Any]:
    """Every log name through the artist task's engines: the local one, then MusicBrainz or
    deferral, with an empty search map (a truncated name is looked up in the cache alone)."""
    names_repo = PgBroadcastArtistRepository(conn)
    names = [
        names_repo._row_to_model(row)
        for row in conn.execute("SELECT * FROM broadcast_artists ORDER BY id").fetchall()
    ]
    hook("names", conn)
    inputs = ArtistEngineInputs(_artist_repos(conn), client, _artist_thresholds())
    local, mb = build_local_artist_engine(inputs), build_mb_artist_engine(inputs, {})
    catalog = _catalog(conn)
    hook("engines", conn)
    stage: dict[str, Any] = {}
    for name in names:
        try:
            result = resolve_one_name(name, local, mb)
        except MusicBrainzUnavailableError:
            stage[str(name.id)] = _unavailable(name.original_name)
            continue
        stage[str(name.id)] = _artist_entry(name.original_name, result, catalog)
    hook("decided", conn)
    return {"artist-stage": stage, "artist-cache": _cache_counts(client)}


# --- identity stage (S4) -------------------------------------------------------------------------


def _identity_repos(conn: Conn) -> IdentityMatchingRepos:
    """The repositories identity_matching_task wires, on the dry run's connection."""
    library_files = PgLibraryFileRepository(conn)
    return IdentityMatchingRepos(
        track_identity_repo=PgBroadcastTrackIdentityRepository(conn),
        broadcast_artist_repo=PgBroadcastArtistRepository(conn),
        match_repo=PgMatchRepository(conn),
        library_file_repo=library_files,
        rules_repo=PgMappingRuleRepository(conn),
        catalog_repo=PgArtistRepository(conn),
        recording_search_reach=library_files,
        link_repo=PgLogNameLinkRepository(conn),
    )


def _line_entry(result: IdentityMatchResult) -> dict[str, Any]:
    writes_file = result.library_file_id is not None and result.status != MatchStatus.AUTO_REJECTED
    return {
        "status": result.status.value,
        "tier": result.tier.value,
        "file": str(result.library_file_id) if writes_file else None,
        "score": result.confidence_score,
        "reason": None if result.reason_code is None else result.reason_code.value,
    }


def identity_stage(conn: Conn, client: MusicBrainzApiClient, hook: Hook) -> dict[str, Any]:
    """Every line of a resolved name through the identity task's engine, artist by artist."""
    lines_repo = PgBroadcastTrackIdentityRepository(conn)
    lines = [
        lines_repo._row_to_model(row)
        for row in conn.execute(_LINES_SQL, {"resolved": list(_RESOLVED)}).fetchall()
    ]
    hook("lines", conn)
    repos = _identity_repos(conn)
    artist_ids = sorted({line.broadcast_artist_id for line in lines}, key=str)
    artists = {a.id: a for a in repos.broadcast_artist_repo.get_by_ids(artist_ids)}
    inputs = IdentityEngineInputs(
        repos=repos,
        mb_client=client,
        strong_match_threshold=get_settings().strong_match_threshold,
        recording_search=RecordingSearch(client, cutoff=sys.maxsize),
        exclusions_by_identity=load_exclusions(lines, repos.library_file_repo),
    )
    engine = build_identity_engine(inputs)
    hook("engine", conn)
    stage = {
        str(line.id): _line_entry(
            resolve_one_line(line, artists.get(line.broadcast_artist_id), engine)
        )
        for line in group_by_artist(lines)
    }
    hook("decided", conn)
    return {"identity-stage": stage, "identity-cache": _cache_counts(client)}


_STAGE_RUNS: dict[str, Callable[[Conn, MusicBrainzApiClient, Hook], dict[str, Any]]] = {
    "artist": artist_stage,
    "identity": identity_stage,
}


def _no_hook(_step: str, _conn: Conn) -> None:
    return None


def run(dsn: str, stage: str, hook: Hook | None = None) -> dict[str, Any]:
    """One stage's dry run on *dsn*: the recorders' document (``header``, ``sections``).

    *hook* is called with the run's own connection after each step, the first after the
    run's first read (the header's), which fixes the snapshot every section reads.
    """
    step = hook or _no_hook
    with gate_common.snapshot_read(dsn) as conn, gate_common.quiet_logging():
        head = gate_common.header(TOOL, FORMAT_VERSION, conn, stage=stage)
        step("header", conn)
        with CacheOnlyMusicBrainzClient(ReadOnlyMusicBrainzCache(conn)) as client:
            sections = _STAGE_RUNS[stage](conn, client, step)
    return {"header": head, "sections": sections}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dsn", help="the gate copy (a bench database)")
    parser.add_argument("--stage", choices=STAGES)
    parser.add_argument("--out", type=Path, help="the dry run's file to write")
    gate_common.add_compare_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.compare:
        return gate_common.run_compare(args, COMPARE)
    if not args.dsn or not args.stage or args.out is None:
        parser.error("--dsn, --stage and --out are required (or --compare)")
    gate_common.point_at(args.dsn)
    started = time.perf_counter()
    doc = run(args.dsn, args.stage)
    gate_common.write_doc(args.out, doc)
    decided = doc["sections"][f"{args.stage}-stage"]
    cache = doc["sections"][f"{args.stage}-cache"]
    print(
        f"{TOOL} {args.stage}: {len(decided)} decided, cache {cache['hits']} hits "
        f"{cache['misses']} misses; {time.perf_counter() - started:.1f} s -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Benchmark library metadata enrichment against a clone of the app database.

Enrichment is one MusicBrainz lookup per distinct release (or recording),
rate-limited to one call every 1.1 s, followed by a few UPDATEs per file.
This harness separates the two: a ``--cold`` run empties the MusicBrainz
cache and measures the real thing, network and all; a ``--warm`` run keeps
the cache the cold run filled, makes no network calls, and so isolates the
local cost (DB round trips, JSON, progress bookkeeping) that code can shrink.

Subcommands:

  prepare  Clone the app DB into the bench DB with pg_dump | pg_restore:
             uv run python scripts/bench_enrich.py prepare
  run      Reset enrichment state in the bench DB, run the enrichment task
           in-process, print timings (JSON):
             uv run python scripts/bench_enrich.py run --label base --cold \
                 --out results/enrich_cold.json
             uv run python scripts/bench_enrich.py run --label base --warm \
                 --out results/enrich_warm.json
           ``--limit N`` resets only the first N releases for a quick check.
           ``--phase both`` also runs the follow-on MusicBrainz enhancement
           task (artists, works, recordings) that the real pipeline chains.
  compare  Diff the DB fingerprints stored in two run results:
             uv run python scripts/bench_enrich.py compare a.json b.json

The run measures a re-enrichment of an already-grouped library: recordings,
works and artists rows exist from the clone, so upserts take their conflict
path, as they do after any rescan. The bench DB (``retrostation_bench_enrich``
by default) is separate from ``bench_scan.py``'s so both harnesses can run at
once; the script refuses any DB name without ``bench`` in it, and the
follow-on Huey task is always stubbed so nothing reaches the real queue.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql as pg_sql

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_scan import (  # noqa: E402
    Timers,
    _default_admin_dsn,
    _git_describe,
    _maybe_profile,
    _patch,
    _quiet_logging,
    _with_dbname,
)

DEFAULT_BENCH_DB = "retrostation_bench_enrich"


def require_bench_name(dbname: str) -> None:
    if "bench" not in dbname:
        raise SystemExit(f"refusing to touch non-bench database {dbname!r}")


# ---------------------------------------------------------------------------
# Timing instrumentation
# ---------------------------------------------------------------------------


def instrument(timers: Timers, phase: str) -> list[str]:
    """Wrap the enrichment path's phases and hot spots. Returns missing names.

    ``phase`` is ``"library"`` or ``"both"``; the follow-on task's internals
    are only wrapped for ``"both"``.
    """
    from backend.db.repositories import artists as pg_artists
    from backend.db.repositories import library_files as pg_files
    from backend.db.repositories import musicbrainz_cache as pg_cache
    from backend.db.repositories import recordings as pg_recordings
    from backend.db.repositories import task_progress as pg_progress
    from backend.db.repositories import works as pg_works
    from backend.services import mb_client as mbc
    from backend.tasks import library_enrichment_tasks as let
    from backend.tasks import mb_enrichment_tasks as met

    missing: list[str] = []

    def patch(owner: Any, attr: str, name: str, within: str | None = None) -> None:
        if not _patch(timers, owner, attr, name, within):
            missing.append(f"{owner.__name__}.{attr}")

    # Top-level phases. The task imports the service functions by name, so
    # they are wrapped where the task looks them up.
    patch(let, "enrich_by_recording_batch", "phase.enrich_by_recording_batch")
    patch(let, "enrich_by_release", "phase.enrich_by_release")
    patch(let, "enrich_by_recording", "phase.enrich_by_recording")

    # MusicBrainz client: cache lookup, then (on a miss) rate-limit wait plus
    # the HTTP round trip. ``mb.fetch`` includes the wait; subtract
    # ``mb.rate_limit_wait`` for network time.
    client = mbc.MusicBrainzApiClient
    patch(client, "search_recordings_by_mbids", "mb.search_recordings_by_mbids",
          "phase.enrich_by_recording_batch")
    patch(client, "lookup_release", "mb.lookup_release", "phase.enrich_by_release")
    patch(client, "lookup_recording", "mb.lookup_recording", "phase.enrich_by_recording")
    patch(client, "_fetch", "mb.fetch", "mb.lookup_*")
    patch(mbc, "_rate_limit", "mb.rate_limit_wait", "mb.fetch")
    patch(pg_cache.PgMusicBrainzCacheRepository, "get", "db.mb_cache.get", "mb.lookup_*")
    patch(pg_cache.PgMusicBrainzCacheRepository, "get_many", "db.mb_cache.get_many",
          "mb.search_recordings_by_mbids")
    patch(pg_cache.PgMusicBrainzCacheRepository, "set", "db.mb_cache.set", "mb.lookup_*")

    # Database round trips inside the per-release / per-recording work.
    for cls, prefix, attrs in (
        (pg_files.PgLibraryFileRepository, "files", (
            "get_pending_enrichment_with_release", "get_pending_enrichment_by_release",
            "get_pending_enrichment_by_recording", "update_recording_link", "update_work_id",
        )),
        (pg_recordings.PgRecordingRepository, "recordings", ("upsert",)),
        (pg_works.PgWorkRepository, "works", ("upsert_from_mb",)),
        (pg_artists.PgArtistRepository, "artists", ("upsert_musicbrainz_artist",)),
    ):
        for attr in attrs:
            patch(cls, attr, f"db.{prefix}.{attr}", "phase.enrich_by_*")

    # Bookkeeping the task does between items: not inside either phase.
    patch(pg_progress.PgTaskProgressRepository, "upsert", "db.progress.upsert")
    patch(psycopg.Connection, "commit", "db.commit")

    if phase == "both":
        patch(met, "_run_artist_phase", "phase.mb_artists")
        patch(met, "_run_works_phase", "phase.mb_works")
        patch(met, "_run_recordings_phase", "phase.mb_recordings")
        patch(met, "coalesce_artist_lookups", "mb.coalesce_artists", "phase.mb_artists")
        patch(met, "coalesce_recording_lookups", "mb.coalesce_recordings",
              "phase.mb_recordings")
        patch(met, "_enhance_artist", "mb.enhance_artist", "phase.mb_artists")
        patch(client, "lookup_artist", "mb.lookup_artist", "phase.mb_artists")
        patch(client, "search_artist", "mb.search_artist", "phase.mb_artists")
        for cls, prefix in (
            (pg_artists.PgArtistRepository, "artists"),
            (pg_works.PgWorkRepository, "works"),
            (pg_recordings.PgRecordingRepository, "recordings"),
        ):
            patch(cls, "mark_enhanced", f"db.{prefix}.mark_enhanced", "phase.mb_*")
    return missing


class ClientCounters:
    """Sum of live fetches and cache hits over every client the run opened."""

    def __init__(self) -> None:
        self.live_fetches = 0
        self.cache_hits = 0

    def install(self) -> None:
        from backend.services.mb_client import MusicBrainzApiClient

        original_exit = MusicBrainzApiClient.__exit__
        counters = self

        def recording_exit(client: Any, *exc: object) -> None:
            counters.live_fetches += client.live_fetches
            counters.cache_hits += client.cache_hits
            original_exit(client, *exc)

        MusicBrainzApiClient.__exit__ = recording_exit  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Keeping the follow-on task off the real queue
# ---------------------------------------------------------------------------


@contextmanager
def follow_on_task(run: bool) -> Iterator[dict[str, Any]]:
    """Replace ``mb_enrichment_task`` for the duration of the block.

    ``library_enrichment_task`` ends by calling ``mb_enrichment_task()``, which
    on a Huey task object *enqueues* it for the worker process, against
    whatever database the worker uses. Here it becomes a no-op, or, when
    ``run`` is set, an in-process ``call_local`` whose result lands in the
    yielded dict under ``"result"``.
    """
    from backend.tasks import mb_enrichment_tasks as met

    original = met.mb_enrichment_task
    captured: dict[str, Any] = {}

    def stub() -> dict[str, int]:
        if not run:
            return {}
        result: dict[str, int] = original.call_local()
        captured["result"] = result
        return result

    met.mb_enrichment_task = stub
    try:
        yield captured
    finally:
        met.mb_enrichment_task = original


# ---------------------------------------------------------------------------
# Bench database
# ---------------------------------------------------------------------------


def _pg_bin(explicit: str | None, tool: str) -> str:
    if explicit:
        return str(Path(explicit) / (f"{tool}.exe" if sys.platform == "win32" else tool))
    found = shutil.which(tool)
    if found:
        return found
    for pattern in (
        "C:/Program Files/PostgreSQL/*/bin", "D:/Program Files/PostgreSQL/*/bin",
    ):
        for bindir in sorted(glob.glob(pattern), reverse=True):
            candidate = Path(bindir) / f"{tool}.exe"
            if candidate.exists():
                return str(candidate)
    raise SystemExit(f"{tool} not found; pass --pg-bin DIR")


def clone_database(source_dsn: str, admin_dsn: str, dbname: str, pg_bin: str | None) -> str:
    """Drop and recreate *dbname*, then stream a pg_dump of *source_dsn* into it."""
    require_bench_name(dbname)
    ident = pg_sql.Identifier(dbname)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(pg_sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(ident))
        admin.execute(pg_sql.SQL("CREATE DATABASE {}").format(ident))
    target_dsn = _with_dbname(admin_dsn, dbname)

    dump = subprocess.Popen(
        [_pg_bin(pg_bin, "pg_dump"), "--format=custom", "--no-owner", "--no-privileges",
         f"--dbname={source_dsn}"],
        stdout=subprocess.PIPE,
    )
    restore = subprocess.run(
        [_pg_bin(pg_bin, "pg_restore"), "--no-owner", "--no-privileges",
         f"--dbname={target_dsn}"],
        stdin=dump.stdout, check=False,
    )
    if dump.stdout is not None:
        dump.stdout.close()
    if dump.wait() != 0:
        raise SystemExit(f"pg_dump exited {dump.returncode}")
    if restore.returncode != 0:
        raise SystemExit(f"pg_restore exited {restore.returncode}")
    return target_dsn


def reset_scope_sql(limit: int | None) -> str:
    """Files the run should enrich: everything with an MBID, or the first N
    releases (and first N release-less recordings) in MBID order."""
    if limit is None:
        return """
            SELECT id FROM library_files
             WHERE release_mbid IS NOT NULL OR recording_mbid IS NOT NULL
        """
    return f"""
        SELECT id FROM library_files
         WHERE release_mbid IN (
                   SELECT DISTINCT release_mbid FROM library_files
                    WHERE release_mbid IS NOT NULL
                    ORDER BY release_mbid LIMIT {limit})
            OR (release_mbid IS NULL AND recording_mbid IN (
                   SELECT DISTINCT recording_mbid FROM library_files
                    WHERE release_mbid IS NULL AND recording_mbid IS NOT NULL
                    ORDER BY recording_mbid LIMIT {limit}))
    """


def reset_enrichment(dsn: str, *, cold: bool, limit: int | None) -> dict[str, int]:
    """Put the bench DB in its pre-run state and return the workload it holds."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        # Park every enrichable pending row first so a --limit run's
        # workload is exactly the chosen releases, nothing left over.
        conn.execute("""
            UPDATE library_files SET enrichment_status = 'failed'
             WHERE enrichment_status = 'pending'
               AND (release_mbid IS NOT NULL OR recording_mbid IS NOT NULL)
        """)
        conn.execute(f"""
            UPDATE library_files
               SET enrichment_status = 'pending', recording_id = NULL
             WHERE id IN ({reset_scope_sql(limit)})
        """)
        cache_rows = conn.execute("SELECT count(*) FROM mb_cache").fetchone()
        if cold:
            conn.execute("TRUNCATE mb_cache")
        row = conn.execute("""
            SELECT count(*) AS pending_files,
                   count(DISTINCT release_mbid) AS distinct_releases,
                   count(DISTINCT recording_mbid)
                       FILTER (WHERE release_mbid IS NULL) AS distinct_recordings
              FROM library_files
             WHERE enrichment_status = 'pending'
               AND (release_mbid IS NOT NULL OR recording_mbid IS NOT NULL)
        """).fetchone()
    assert row is not None and cache_rows is not None
    return {
        "pending_files": row[0],
        "distinct_releases": row[1],
        "distinct_recordings": row[2],
        "mb_cache_rows_before": 0 if cold else cache_rows[0],
    }


def point_app_at(dsn: str) -> None:
    """Make ``get_settings().database_url`` return *dsn* for the rest of the process.

    The task opens its own connections from settings, so this is how the
    in-process run lands in the bench DB. Must run before any task module is
    imported: ``huey_app`` reads settings at import.
    """
    require_bench_name(str(psycopg.conninfo.conninfo_to_dict(dsn)["dbname"]))
    os.environ["DATABASE_URL"] = dsn
    from backend.config import get_settings

    get_settings.cache_clear()
    if get_settings().database_url != dsn:
        raise SystemExit("settings did not pick up the bench DATABASE_URL")


# ---------------------------------------------------------------------------
# Fingerprint: what "the same enrichment" means
# ---------------------------------------------------------------------------

_FINGERPRINT_QUERIES: dict[str, str] = {
    "library_files": """
        SELECT file_path, enrichment_status, recording_id, work_id
          FROM library_files ORDER BY file_path
    """,
    "recordings": """
        SELECT id, title, work_id, duration_ms, version_type
          FROM recordings ORDER BY id
    """,
    "works": "SELECT id, title, artist_id, origin FROM works ORDER BY id",
    "artists": "SELECT id, name, sort_name, mbid FROM artists ORDER BY id",
}


def fingerprint(dsn: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    with psycopg.connect(dsn) as conn:
        for table, query in _FINGERPRINT_QUERIES.items():
            rows = [
                [None if v is None else str(v) for v in row]
                for row in conn.execute(query).fetchall()
            ]
            digest = hashlib.sha256(json.dumps(rows).encode("utf-8")).hexdigest()
            out[table] = {"count": len(rows), "sha256": digest, "rows": rows}
    return out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_prepare(args: argparse.Namespace) -> None:
    from backend.config import get_settings

    source = get_settings().database_url
    admin = args.admin_dsn or _default_admin_dsn()
    start = time.perf_counter()
    dsn = clone_database(source, admin, args.db, args.pg_bin)
    with psycopg.connect(dsn) as conn:
        statuses = conn.execute(
            "SELECT enrichment_status, count(*) FROM library_files GROUP BY 1 ORDER BY 1"
        ).fetchall()
        cache = conn.execute("SELECT count(*) FROM mb_cache").fetchone()
    print(json.dumps({
        "db": args.db,
        "cloned_in_s": round(time.perf_counter() - start, 1),
        "library_files": {status: n for status, n in statuses},
        "mb_cache_rows": cache[0] if cache else None,
    }, indent=2))


def cmd_run(args: argparse.Namespace) -> None:
    require_bench_name(args.db)
    admin = args.admin_dsn or _default_admin_dsn()
    dsn = _with_dbname(admin, args.db)
    point_app_at(dsn)

    workload = reset_enrichment(dsn, cold=args.cold, limit=args.limit)
    print(json.dumps({"workload": workload}), flush=True)
    if workload["pending_files"] == 0:
        raise SystemExit("nothing to enrich; run `prepare` first")

    from backend.tasks.library_enrichment_tasks import library_enrichment_task

    # After the import: huey_app configures logging when it loads.
    _quiet_logging()

    timers = Timers()
    missing_patches: list[str] = []
    if not args.no_instrument:
        missing_patches = instrument(timers, args.phase)
        if missing_patches:
            print(f"not instrumented (missing): {', '.join(missing_patches)}", flush=True)
    counters = ClientCounters()
    counters.install()

    start = time.perf_counter()
    with follow_on_task(run=args.phase == "both") as follow_on, _maybe_profile(args.profile):
        outcome: dict[str, int] = library_enrichment_task.call_local()
    elapsed = time.perf_counter() - start

    lookups = workload["distinct_releases"] + workload["distinct_recordings"]
    result: dict[str, Any] = {
        "label": args.label,
        "git": _git_describe(),
        "db": args.db,
        "cache": "cold" if args.cold else "warm",
        "phase": args.phase,
        "workload": workload,
        "elapsed_s": round(elapsed, 3),
        "enriched": outcome.get("enriched"),
        "failed": outcome.get("failed"),
        "follow_on": follow_on.get("result"),
        "mb": {"live_fetches": counters.live_fetches, "cache_hits": counters.cache_hits},
        "s_per_lookup": round(elapsed / lookups, 3) if lookups else None,
        "ms_per_file": (
            round(elapsed / workload["pending_files"] * 1000, 2)
            if workload["pending_files"] else None
        ),
        "timers": timers.report(),
        "missing_patches": missing_patches,
    }
    if not args.no_fingerprint:
        result["fingerprint"] = fingerprint(dsn)

    summary = {k: v for k, v in result.items() if k not in ("fingerprint", "timers")}
    print(json.dumps(summary, indent=2))
    print(json.dumps(result["timers"], indent=2))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(result, indent=1), encoding="utf-8")


def cmd_compare(args: argparse.Namespace) -> None:
    a = json.loads(Path(args.a).read_text(encoding="utf-8"))
    b = json.loads(Path(args.b).read_text(encoding="utf-8"))
    fa, fb = a["fingerprint"], b["fingerprint"]
    identical = True
    for table in _FINGERPRINT_QUERIES:
        ta, tb = fa[table], fb[table]
        same = ta["sha256"] == tb["sha256"]
        identical &= same
        status = "identical" if same else "DIFFERENT"
        print(f"{table:20s} {ta['count']:>7} vs {tb['count']:>7}  {status}")
        if not same:
            ra = {json.dumps(r) for r in ta["rows"]}
            rb = {json.dumps(r) for r in tb["rows"]}
            for row in sorted(ra - rb)[: args.show]:
                print(f"   - {row[:300]}")
            for row in sorted(rb - ra)[: args.show]:
                print(f"   + {row[:300]}")
    print(f"\n{a['label']} ({a['cache']}): {a['elapsed_s']}s   "
          f"{b['label']} ({b['cache']}): {b['elapsed_s']}s   "
          f"speed-up x{a['elapsed_s'] / b['elapsed_s']:.2f}")
    if not identical:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prep = sub.add_parser("prepare")
    p_prep.add_argument("--db", default=DEFAULT_BENCH_DB)
    p_prep.add_argument(
        "--admin-dsn",
        help="maintenance-DB DSN (default: DATABASE_URL with dbname 'postgres')",
    )
    p_prep.add_argument("--pg-bin", help="directory holding pg_dump and pg_restore")
    p_prep.set_defaults(func=cmd_prepare)

    p_run = sub.add_parser("run")
    p_run.add_argument("--label", required=True)
    p_run.add_argument("--out")
    p_run.add_argument("--db", default=DEFAULT_BENCH_DB)
    p_run.add_argument("--admin-dsn")
    p_run.add_argument("--limit", type=int, help="reset only the first N releases")
    p_run.add_argument("--phase", choices=("library", "both"), default="library")
    p_run.add_argument("--profile", help="write cProfile stats to this path")
    p_run.add_argument("--no-instrument", action="store_true")
    p_run.add_argument("--no-fingerprint", action="store_true")
    cache = p_run.add_mutually_exclusive_group(required=True)
    cache.add_argument("--cold", action="store_true",
                       help="empty the MusicBrainz cache first (live network, rate-limited)")
    cache.add_argument("--warm", action="store_true",
                       help="keep the cache (no network; measures local cost only)")
    p_run.set_defaults(func=cmd_run)

    p_cmp = sub.add_parser("compare")
    p_cmp.add_argument("a")
    p_cmp.add_argument("b")
    p_cmp.add_argument("--show", type=int, default=5)
    p_cmp.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

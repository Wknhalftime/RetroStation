"""Benchmark a full library scan into an empty database.

Measures what a first scan of a library costs, phase by phase, and
fingerprints the resulting database so two scanner versions can be shown
to produce the same library, not merely to finish sooner.

Subcommands:

  corpus   Build junction farms of real artist folders (no copying):
             uv run python scripts/bench_scan.py corpus --out D:/bench \
                 --source D:/Media/Music/Albums --sets warm=1200,cold_a=500
  run      Reset the bench DB, migrate, scan ROOT, print timings (JSON):
             uv run python scripts/bench_scan.py run --root D:/bench/warm \
                 --label baseline --out results/baseline.json
  compare  Diff the DB fingerprints stored in two run results:
             uv run python scripts/bench_scan.py compare a.json b.json

The bench DB (``retrostation_bench`` by default) is dropped and rebuilt on
every run; the script refuses any DB name without ``bench`` in it.
"""

from __future__ import annotations

import argparse
import cProfile
import functools
import hashlib
import importlib.util
import io
import json
import os
import pstats
import random
import subprocess
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql as pg_sql

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import get_settings  # noqa: E402
from backend.services.library_scan_service import SUPPORTED_EXTENSIONS  # noqa: E402

DEFAULT_BENCH_DB = "retrostation_bench"


# ---------------------------------------------------------------------------
# Timing instrumentation
# ---------------------------------------------------------------------------


class Timers:
    """Thread-safe accumulating wall-clock timers keyed by name.

    Timers nest: a row's ``within`` names the enclosing timer whose time
    already includes it (``file.sha256`` runs inside ``file.extract_tags``,
    which runs inside ``phase.scan_directory``). Only rows with the same
    ``within`` are disjoint, so sum siblings, never the whole report.

    Time in a worker thread is summed per call, so a timer wrapped around
    parallel work can exceed the elapsed wall time; that is reported as-is.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.seconds: dict[str, float] = defaultdict(float)
        self.calls: dict[str, int] = defaultdict(int)
        self.within: dict[str, str | None] = {}

    def add(self, name: str, elapsed: float) -> None:
        with self._lock:
            self.seconds[name] += elapsed
            self.calls[name] += 1

    def wrap(
        self, name: str, fn: Callable[..., Any], within: str | None = None,
    ) -> Callable[..., Any]:
        self.within[name] = within

        @functools.wraps(fn)
        def timed(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                self.add(name, time.perf_counter() - start)

        return timed

    def report(self) -> dict[str, dict[str, Any]]:
        return {
            k: {"seconds": round(v, 3), "calls": self.calls[k], "within": self.within.get(k)}
            for k, v in sorted(self.seconds.items(), key=lambda kv: -kv[1])
        }


def _patch(
    timers: Timers, owner: Any, attr: str, name: str, within: str | None = None,
) -> bool:
    """Wrap ``owner.attr`` in a timer. Returns False if the name is missing.

    Scanner versions differ, so a missing hot spot is tolerated here; the
    caller reports it so a renamed function does not silently vanish from
    the timings and get read as "now costs nothing".
    """
    fn = getattr(owner, attr, None)
    if fn is None or not callable(fn):
        return False
    setattr(owner, attr, timers.wrap(name, fn, within))
    return True


def instrument(timers: Timers) -> list[str]:
    """Wrap the scan's phases and hot spots. Returns the names it could not find."""
    import mutagen

    from backend.db.repositories import artists as pg_artists
    from backend.db.repositories import library_files as pg_files
    from backend.db.repositories import library_folders as pg_folders
    from backend.db.repositories import library_quarantine as pg_quar
    from backend.db.repositories import recordings as pg_recordings
    from backend.db.repositories import song_masters as pg_masters
    from backend.db.repositories import works as pg_works
    from backend.services import folder_hash_service as fhs
    from backend.services import grouping_service as gs
    from backend.services import library_scan_service as lss
    from backend.tasks import library_scan_tasks as lst

    missing: list[str] = []

    def patch(owner: Any, attr: str, name: str, within: str | None = None) -> bool:
        found = _patch(timers, owner, attr, name, within)
        if not found:
            missing.append(f"{owner.__name__}.{attr}")
        return found

    def patch_any(
        owner: Any, attrs: tuple[str, ...], name: str, within: str | None = None,
    ) -> None:
        """Time whichever of *attrs* exists on *owner*.

        A rename (``_compute_file_hash`` -> ``compute_file_hash``) must not
        make the timer vanish, since old and new scanner versions share this
        harness; only report missing when neither name is found.
        """
        if not any(_patch(timers, owner, attr, name, within) for attr in attrs):
            missing.append(f"{owner.__name__}.{'/'.join(attrs)}")

    # Top-level phases, as seen from the task module. Disjoint with each other.
    for attr in (
        "scan_directory", "mark_unseen_missing", "clear_resolved_quarantine",
        "diff_tree", "assign_work",
    ):
        patch(lst, attr, f"phase.{attr}")

    # Per-file extraction work (summed over worker threads if parallel).
    patch(lss, "extract_tags", "file.extract_tags", "phase.scan_directory")
    # A tags-only first scan calls read_tags directly, never extract_tags, so
    # this is what makes its per-file cost visible. Missing on scanner
    # versions before the two-phase split — that is fine and gets reported.
    has_read_tags = patch(lss, "read_tags", "file.read_tags", "phase.scan_directory")
    # disk_stat (and, before extract_tags/read_tags split, mutagen.File) runs
    # inside read_tags on versions that have it, inside extract_tags on those
    # that don't; sha256 always runs in extract_tags, after read_tags returns.
    inner_within = "file.read_tags" if has_read_tags else "file.extract_tags"
    patch_any(lss, ("compute_file_hash", "_compute_file_hash"), "file.sha256", "file.extract_tags")
    patch_any(lss, ("disk_stat", "_disk_stat"), "file.stat", inner_within)
    # The service calls mutagen.File through the module, so patch it there.
    patch(mutagen, "File", "file.mutagen_parse", inner_within)

    # The backfill (absent before the two-phase split; skipped silently then).
    if importlib.util.find_spec("backend.services.hash_backfill_service") is not None:
        from backend.services import hash_backfill_service as hbs

        patch(hbs, "_backfill_one", "backfill.file", "phase.backfill")
        # hash_file is a keyword-only default bound to compute_file_hash at
        # def time, and cmd_run's import chain (_run_scan -> huey_app ->
        # library_hash_backfill_tasks -> hash_backfill_service) pulls this
        # module in before instrument() runs, so it is already holding that
        # reference: patching library_scan_service.compute_file_hash afterwards
        # cannot reach it. Only overwriting the default itself does.
        kwdefaults = hbs.backfill_hash_batch.__kwdefaults__
        if kwdefaults is not None and "hash_file" in kwdefaults:
            kwdefaults["hash_file"] = timers.wrap(
                "backfill.sha256", kwdefaults["hash_file"], "backfill.file"
            )
        else:
            missing.append("hash_backfill_service.backfill_hash_batch.hash_file")

    # Folder tree.
    patch(fhs, "compute_folder_hash", "tree.compute_folder_hash", "phase.diff_tree")
    patch(fhs, "_walk_folder_paths", "tree.walk", "phase.diff_tree")

    # Grouping internals.
    for attr in (
        "_try_hash_shortcut", "_try_mbid_shortcut", "_fuzzy_match_work",
        "_create_local_work",
    ):
        patch(gs, attr, f"group.{attr}", "phase.assign_work")

    # Database round trips, by repository method. Each runs inside whichever
    # phase (or grouping step) called it, so they are not disjoint with those.
    for cls, prefix in (
        (pg_files.PgLibraryFileRepository, "db.files"),
        (pg_folders.PgLibraryFolderRepository, "db.folders"),
        (pg_quar.PgLibraryQuarantineRepository, "db.quarantine"),
        (pg_artists.PgArtistRepository, "db.artists"),
        (pg_works.PgWorkRepository, "db.works"),
        (pg_recordings.PgRecordingRepository, "db.recordings"),
        (pg_masters.PgSongMasterRepository, "db.song_masters"),
    ):
        for attr, value in list(vars(cls).items()):
            if callable(value) and not attr.startswith("_"):
                setattr(cls, attr, timers.wrap(f"{prefix}.{attr}", value, "phase.*"))

    return missing


# ---------------------------------------------------------------------------
# Bench database
# ---------------------------------------------------------------------------


def _with_dbname(dsn: str, dbname: str) -> str:
    params = psycopg.conninfo.conninfo_to_dict(dsn)
    params["dbname"] = dbname
    return psycopg.conninfo.make_conninfo(**params)


def _default_admin_dsn() -> str:
    """The app's DATABASE_URL pointed at the maintenance DB, so the harness
    reaches the same server and credentials the app and tests use."""
    return _with_dbname(get_settings().database_url, "postgres")


def reset_bench_db(admin_dsn: str, dbname: str) -> str:
    """Create *dbname* if needed, wipe its schema, migrate. Returns its DSN."""
    if "bench" not in dbname:
        raise SystemExit(f"refusing to reset non-bench database {dbname!r}")
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if not exists:
            admin.execute(pg_sql.SQL("CREATE DATABASE {}").format(pg_sql.Identifier(dbname)))
    dsn = _with_dbname(admin_dsn, dbname)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
    from backend.db.migrations import run_migrations

    with psycopg.connect(dsn) as conn:
        run_migrations(conn)
    return dsn


# ---------------------------------------------------------------------------
# Fingerprint: what "the same library" means
# ---------------------------------------------------------------------------

_FINGERPRINT_QUERIES: dict[str, str] = {
    # Every tag, stat and hash we extracted, per path.
    "library_files": """
        SELECT f.file_path, f.file_hash, f.format, f.enrichment_status, f.file_status,
               f.recording_mbid, f.artist_mbid, f.album_artist_mbid, f.release_mbid,
               f.release_title, f.release_type, f.release_status, f.track_title,
               f.track_number, f.disc_number, f.duration_ms, f.bitrate,
               f.raw_metadata::text AS raw_metadata, f.artist_name,
               f.normalized_artist_name, f.normalized_title, f.file_size, f.file_mtime_ns,
               w.title AS work_title, wa.name AS work_artist,
               r.title AS recording_title, r.version_type AS recording_version
          FROM library_files f
          LEFT JOIN works w ON w.id = f.work_id
          LEFT JOIN artists wa ON wa.id = w.artist_id
          LEFT JOIN recordings r ON r.id = f.recording_id
         ORDER BY f.file_path
    """,
    # Works are identified by (artist, title) since ids are random per run.
    "works": """
        SELECT a.name AS artist, a.normalized_name, w.title, w.origin,
               pf.file_path AS preferred_file
          FROM works w
          JOIN artists a ON a.id = w.artist_id
          LEFT JOIN song_masters sm ON sm.work_id = w.id
          LEFT JOIN library_files pf ON pf.id = sm.preferred_file_id
         ORDER BY a.name, w.title, pf.file_path
    """,
    "recordings": """
        SELECT a.name AS artist, w.title AS work, r.title, r.version_type
          FROM recordings r
          JOIN works w ON w.id = r.work_id
          JOIN artists a ON a.id = w.artist_id
         ORDER BY a.name, w.title, r.title, r.version_type
    """,
    "library_folders": """
        SELECT f.full_path, f.name, f.folder_hash, p.full_path AS parent
          FROM library_folders f
          LEFT JOIN library_folders p ON p.id = f.parent_id
         ORDER BY f.full_path
    """,
    "library_quarantine": """
        SELECT file_path, error_message FROM library_quarantine ORDER BY file_path
    """,
}


def fingerprint(dsn: str) -> dict[str, Any]:
    """Row counts and a digest per table; rows kept for diffing."""
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
# run
# ---------------------------------------------------------------------------


@contextmanager
def _maybe_profile(path: str | None) -> Iterator[None]:
    if path is None:
        yield
        return
    prof = cProfile.Profile()
    prof.enable()
    try:
        yield
    finally:
        prof.disable()
        prof.dump_stats(path)
        buf = io.StringIO()
        pstats.Stats(prof, stream=buf).sort_stats("cumulative").print_stats(40)
        Path(path).with_suffix(".txt").write_text(buf.getvalue(), encoding="utf-8")


def _audio_files(root: Path) -> list[str]:
    """Every file under *root* the scanner would pick up, in path order."""
    return sorted(
        os.path.join(dirpath, name)
        for dirpath, _dirs, names in os.walk(root)
        for name in names
        if os.path.splitext(name)[1].lower() in SUPPORTED_EXTENSIONS
    )


def _corpus_stats(files: list[str]) -> dict[str, int]:
    return {"files": len(files), "bytes": sum(os.stat(f).st_size for f in files)}


def _require_windows(feature: str) -> None:
    if sys.platform != "win32":
        raise SystemExit(f"{feature} needs Windows (NTFS and the Win32 file API)")


def _extended_path(path: str) -> str:
    r"""Absolute path with the ``\\?\`` prefix so Win32 accepts it past MAX_PATH."""
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def evict_from_cache(files: list[str]) -> list[str]:
    """Drop *files* from the Windows file cache so the next read hits the disk.

    NTFS purges a file's cached pages when it is opened without buffering
    and no cached handle is open. User-mode only; changes no system setting.
    Lets two scanner versions read the *same* files cold, which matters:
    on some drives cold read speed varies several-fold between folders.

    Returns the files that could not be opened, and so are still cached.
    """
    _require_windows("--evict")
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    generic_read, share_rw, open_existing, no_buffering = 0x80000000, 3, 3, 0x20000000
    invalid = wintypes.HANDLE(-1).value
    still_cached: list[str] = []
    for path in files:
        handle = k32.CreateFileW(
            _extended_path(path), generic_read, share_rw, None,
            open_existing, no_buffering, None,
        )
        if handle == invalid:
            still_cached.append(path)
        else:
            k32.CloseHandle(handle)
    return still_cached


def preload_into_cache(files: list[str]) -> None:
    """Read every byte once so the run measures CPU and DB cost, not the disk."""
    buf = bytearray(1 << 20)
    for path in files:
        with open(path, "rb", buffering=0) as fh:
            while fh.readinto(buf):
                pass


def _git_describe() -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    dirty = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain", "--", "backend"],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() + ("+dirty" if dirty.stdout.strip() else "")


def _quiet_logging() -> None:
    """Drop scan INFO logs so the console carries only the results.

    Call after importing the backend: its import configures logging.
    """
    import logging

    import structlog

    logging.getLogger().setLevel(logging.WARNING)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    )


def _drain_backfill(conn: psycopg.Connection[Any]) -> float | None:
    """Run the deferred-hash backfill to completion; seconds taken.

    None on scanner versions without one, so old and new versions share
    this harness and their fingerprints stay comparable.
    """
    if importlib.util.find_spec("backend.tasks.library_hash_backfill_tasks") is None:
        return None
    from backend.services.repository_factory import RepositoryFactory
    from backend.tasks.library_hash_backfill_tasks import (
        BackfillRunConfig,
        run_hash_backfill,
    )

    start = time.perf_counter()
    repos = RepositoryFactory(conn)
    run_hash_backfill(
        repos.library_files, repos.task_progress, conn.commit,
        BackfillRunConfig(run_id="bench"),
    )
    return time.perf_counter() - start


def cmd_run(args: argparse.Namespace) -> None:
    from backend.db.repositories.task_progress import PgTaskProgressRepository
    from backend.db.sync_conn import connect_sync
    from backend.services.repository_factory import RepositoryFactory
    from backend.tasks.library_scan_tasks import _run_scan

    _quiet_logging()

    root = Path(args.root)
    audio = _audio_files(root)
    corpus = _corpus_stats(audio)
    if args.evict:
        still_cached = evict_from_cache(audio)
        print(f"evicted {len(audio) - len(still_cached)}/{len(audio)} files", flush=True)
        if still_cached:
            # A partly warm "cold" run would silently flatter the candidate.
            listed = "\n  ".join(still_cached[:10])
            raise SystemExit(
                f"{len(still_cached)} files could not be evicted, e.g.:\n  {listed}"
            )
    elif args.preload:
        preload_into_cache(audio)
    dsn = reset_bench_db(args.admin_dsn or _default_admin_dsn(), args.db)

    timers = Timers()
    missing_patches: list[str] = []
    if not args.no_instrument:
        missing_patches = instrument(timers)
        if missing_patches:
            print(f"not instrumented (missing): {', '.join(missing_patches)}", flush=True)

    progress_conn = connect_sync(dsn, autocommit=True)
    library_conn = connect_sync(dsn, autocommit=False)
    try:
        repos = RepositoryFactory(library_conn)
        start = time.perf_counter()
        with _maybe_profile(args.profile):
            written, quarantined, _progress = _run_scan(
                root_path=str(root),
                library_conn=library_conn,
                repos=repos,
                progress_repo=PgTaskProgressRepository(progress_conn),
                task_id=f"bench-{args.label}",
            )
            library_conn.commit()
            phase1 = time.perf_counter() - start
            backfill = None if args.no_drain else _drain_backfill(library_conn)
            if backfill is not None:
                # _drain_backfill is timed by hand, not via timers.wrap(), so
                # record it explicitly — this is what backfill.file/
                # backfill.sha256 (patched in instrument()) nest within.
                timers.add("phase.backfill", backfill)
        elapsed = time.perf_counter() - start
    finally:
        library_conn.close()
        progress_conn.close()

    result: dict[str, Any] = {
        "label": args.label,
        "git": _git_describe(),
        "cache": "evicted" if args.evict else "preloaded" if args.preload else "as-is",
        "root": str(root),
        "corpus": corpus,
        "elapsed_s": round(elapsed, 3),
        "phase1_s": round(phase1, 3),
        "backfill_s": round(backfill, 3) if backfill is not None else None,
        "files_written": written,
        "quarantined": quarantined,
        "files_per_s": round(written / elapsed, 1) if elapsed else None,
        "mb_per_s": (
            round(corpus["bytes"] / elapsed / 1e6, 1) if corpus and elapsed else None
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


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


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
    print(f"time to usable (phase 1): {a['label']}: {a.get('phase1_s')}s   "
          f"{b['label']}: {b.get('phase1_s')}s")
    print(f"\n{a['label']}: {a['elapsed_s']}s   {b['label']}: {b['elapsed_s']}s   "
          f"speed-up x{a['elapsed_s'] / b['elapsed_s']:.2f}")
    if not identical:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# corpus
# ---------------------------------------------------------------------------


def cmd_corpus(args: argparse.Namespace) -> None:
    """Draw disjoint random sets of artist folders and link each set's folders
    into its own directory with NTFS junctions (``mklink /J``)."""
    _require_windows("corpus")
    source = Path(args.source)
    artists = sorted(p for p in source.iterdir() if p.is_dir())
    random.Random(args.seed).shuffle(artists)
    targets = [
        (name, int(n)) for name, n in (s.split("=") for s in args.sets.split(","))
    ]
    out = Path(args.out)
    manifest: dict[str, Any] = {}
    cursor = 0
    for name, target in targets:
        set_dir = out / name
        set_dir.mkdir(parents=True, exist_ok=True)
        picked: list[str] = []
        files = size = 0
        while files < target and cursor < len(artists):
            artist = artists[cursor]
            cursor += 1
            stats = _corpus_stats(_audio_files(artist))
            n, s = stats["files"], stats["bytes"]
            if n == 0 or n > target // 3:  # keep sets made of many artists
                continue
            link = set_dir / artist.name
            if not link.exists():
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(artist)],
                    check=True, capture_output=True,
                )
            picked.append(artist.name)
            files += n
            size += s
        manifest[name] = {"artists": len(picked), "files": files, "bytes": size}
        print(f"{name}: {len(picked)} artists, {files} files, {size / 1e9:.1f} GB")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_corpus = sub.add_parser("corpus")
    p_corpus.add_argument("--source", required=True)
    p_corpus.add_argument("--out", required=True)
    p_corpus.add_argument("--sets", required=True, help="name=files,name=files,...")
    p_corpus.add_argument("--seed", type=int, default=20260925)
    p_corpus.set_defaults(func=cmd_corpus)

    p_run = sub.add_parser("run")
    p_run.add_argument("--root", required=True)
    p_run.add_argument("--label", required=True)
    p_run.add_argument("--out")
    p_run.add_argument("--db", default=DEFAULT_BENCH_DB)
    p_run.add_argument(
        "--admin-dsn",
        help="maintenance-DB DSN (default: DATABASE_URL with dbname 'postgres')",
    )
    p_run.add_argument("--profile", help="write cProfile stats to this path")
    p_run.add_argument("--no-instrument", action="store_true")
    p_run.add_argument("--no-fingerprint", action="store_true")
    p_run.add_argument("--no-drain", action="store_true",
                       help="skip the deferred-hash backfill (time phase 1 alone)")
    cache = p_run.add_mutually_exclusive_group()
    cache.add_argument("--evict", action="store_true",
                       help="drop the corpus from the OS file cache first (cold run)")
    cache.add_argument("--preload", action="store_true",
                       help="read the corpus into the OS file cache first (warm run)")
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

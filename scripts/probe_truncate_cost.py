"""One-shot probe: measures handshake-vs-TRUNCATE cost on the test DB.

Run: uv run python scripts/probe_truncate_cost.py

Prints min/median/p95 (ms) for:
  1. Fresh `psycopg.connect(url, autocommit=True)`  -> handshake band
  2. Reused-connection `TRUNCATE ... CASCADE`       -> truncate band

The probe applies schema migrations itself on start (idempotent, via
`backend.db.migrations.run_migrations`), so the target database only needs
to exist — no prior pytest run is required. The DB itself is NOT created
by this script; create `retrostation_test` beforehand if it is missing.

DESTRUCTIVE: this script runs TRUNCATE ... CASCADE on every domain table
in the target database. It refuses to run unless the DB name starts with
`retrostation_test` (the convention for pytest DBs, including per-xdist-
worker suffixes like `retrostation_test_gw0`). If you must target a
different database (e.g. in CI), set PROBE_CONFIRM_DESTRUCTIVE=1 to
override the guard; NEVER set that variable in an environment that has
access to production data.
"""

from __future__ import annotations

import os
import statistics
import sys
import time

import psycopg

from backend.db.migrations import run_migrations

URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://retrostation:retrostation-dev@localhost:5432/retrostation_test",
)
_TEST_DBNAME_PREFIX = "retrostation_test"
ITERATIONS = 10
TRUNCATE_SQL = """
    TRUNCATE play_events, track_identities, broadcast_artists,
             playlists, broadcast_days, stations,
             matches, mapping_rules,
             artists, works, recordings,
             library_files, library_quarantine,
             song_masters, format_overrides,
             mb_cache, progress_tracking, user_settings,
             system_logs,
             library_folder_staged_hashes, library_folders
    CASCADE
"""


def _is_test_database(url: str) -> bool:
    """True iff the URL's dbname starts with `retrostation_test`.

    Uses psycopg's conninfo parser so both URI and keyword-value forms are
    recognised. Any parse failure is treated as unsafe.
    """
    try:
        params = psycopg.conninfo.conninfo_to_dict(url)
    except psycopg.Error:
        return False
    dbname = str(params.get("dbname", ""))
    return dbname.startswith(_TEST_DBNAME_PREFIX)


def _assert_safe_to_run(url: str) -> None:
    """Refuse to proceed unless the target DB is recognised as a test DB.

    Override with PROBE_CONFIRM_DESTRUCTIVE=1 when the DB name is controlled
    but differs from the default convention (e.g. in a sandboxed CI job).
    """
    if _is_test_database(url):
        return
    if os.environ.get("PROBE_CONFIRM_DESTRUCTIVE") == "1":
        print(
            "WARNING: probe_truncate_cost running against a non-test DB with "
            "PROBE_CONFIRM_DESTRUCTIVE=1.",
            file=sys.stderr,
        )
        return
    print(
        "REFUSED: DATABASE_URL does not point at a `retrostation_test*` database.\n"
        f"  url: {url}\n"
        "  This script runs TRUNCATE ... CASCADE on every domain table. Refusing\n"
        "  to proceed. If this really is a throwaway DB, set "
        "PROBE_CONFIRM_DESTRUCTIVE=1.",
        file=sys.stderr,
    )
    sys.exit(2)


def _ms(seconds: float) -> float:
    return seconds * 1000.0


def _stats(samples: list[float]) -> tuple[float, float, float]:
    samples_sorted = sorted(samples)
    p95_idx = max(0, int(round(0.95 * (len(samples_sorted) - 1))))
    return min(samples_sorted), statistics.median(samples_sorted), samples_sorted[p95_idx]


def probe_handshake(url: str, n: int) -> list[float]:
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        conn = psycopg.connect(url, autocommit=True)
        samples.append(_ms(time.perf_counter() - t0))
        conn.close()
    return samples


def probe_truncate(url: str, n: int) -> list[float]:
    samples: list[float] = []
    with psycopg.connect(url, autocommit=True) as conn:
        # warm-up once (plan-cache, buffer pages)
        conn.execute(TRUNCATE_SQL)
        for _ in range(n):
            t0 = time.perf_counter()
            conn.execute(TRUNCATE_SQL)
            samples.append(_ms(time.perf_counter() - t0))
    return samples


def _ensure_migrated(url: str) -> None:
    """Apply migrations on the target DB so probe_truncate finds real tables.

    run_migrations uses a schema_migrations table to skip already-applied
    versions, so this is cheap on a warm DB and correct on a cold one.
    """
    with psycopg.connect(url) as conn:
        run_migrations(conn)
        conn.commit()


def main() -> None:
    _assert_safe_to_run(URL)
    _ensure_migrated(URL)

    print(f"probe url: {URL}")
    print(f"iterations: {ITERATIONS}\n")

    hs = probe_handshake(URL, ITERATIONS)
    tr = probe_truncate(URL, ITERATIONS)

    for label, samples in (("handshake (connect)", hs), ("truncate cascade", tr)):
        mn, md, p95 = _stats(samples)
        print(f"{label:24} min={mn:7.2f} ms  median={md:7.2f} ms  p95={p95:7.2f} ms")

    hs_median = statistics.median(hs)
    tr_median = statistics.median(tr)
    ratio = tr_median / hs_median if hs_median else float("inf")
    print(f"\ntruncate / handshake median ratio: {ratio:.2f}x")
    if ratio < 1.0:
        print("=> handshake dominates; Tier A amortization is the right lever.")
    elif ratio < 3.0:
        print("=> comparable; Tier A + pg tuning both worth considering.")
    else:
        print("=> TRUNCATE dominates; Tier A saving will be small. See \u00a710 (pg tuning).")


if __name__ == "__main__":
    main()

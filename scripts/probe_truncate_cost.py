"""One-shot probe: measures handshake-vs-TRUNCATE cost on the test DB.

Run: uv run python scripts/probe_truncate_cost.py

Prints min/median/p95 (ms) for:
  1. Fresh `psycopg.connect(url, autocommit=True)`  -> handshake band
  2. Reused-connection `TRUNCATE ... CASCADE`       -> truncate band

Does not modify the schema. Requires `retrostation_test` DB to already be
migrated (run `uv run pytest tests/conftest.py -q --co` once, or any
pytest session -n0, to trigger the session-scope migration fixture).
"""

from __future__ import annotations

import os
import statistics
import time

import psycopg

URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://retrostation:retrostation-dev@localhost:5432/retrostation_test",
)
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


def main() -> None:
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

"""Give the live-version backfill's MusicBrainz cache entries the app's lifetime.

scripts/backfill_live_versions.py built its MusicBrainz client without the
app's cache lifetime (fixed since), so the recording searches it cached on
2026-10-12 got the client's 30-day default and expire on 2026-11-11. The
app keeps the same entries for ``mb_cache_ttl_days`` (3650). This moves
each one's expiry to that many days after it was cached, so the backfill's
searches are not fetched again.

Every other writer of recording-by-mbid entries passes the app's lifetime,
so a recording-by-mbid entry that lives exactly 30 days is the backfill's.
Nothing else is touched, and a second run finds nothing to do. Entries that
have already expired are extended too: nothing deletes expired cache rows.

Run: uv run python scripts/extend_backfill_cache_ttl.py            # dry run: count only
     uv run python scripts/extend_backfill_cache_ttl.py --apply    # extend them

Targets ``DATABASE_URL`` if set, else the app's configured database.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import Settings  # noqa: E402

_KEY_PREFIX = "recording-by-mbid:"
# The client's default lifetime, which the backfill fell back to.
_BACKFILL_LIFETIME = timedelta(days=30)

_FIND_SQL = """
    SELECT count(*), min(cached_at), max(cached_at)
      FROM mb_cache
     WHERE starts_with(cache_key, %s) AND expires_at - cached_at = %s
"""
_EXTEND_SQL = """
    UPDATE mb_cache
       SET expires_at = cached_at + make_interval(days => %s)
     WHERE starts_with(cache_key, %s) AND expires_at - cached_at = %s
"""


@dataclass(frozen=True)
class BackfillEntries:
    count: int
    first_cached: datetime | None
    last_cached: datetime | None


def find_backfill_entries(conn: psycopg.Connection[Any]) -> BackfillEntries:
    """How many of the backfill's entries still have its 30-day lifetime, and when cached."""
    row = conn.execute(_FIND_SQL, (_KEY_PREFIX, _BACKFILL_LIFETIME)).fetchone()
    if row is None:  # an aggregate always returns a row
        raise RuntimeError("count query returned no row")
    count, first_cached, last_cached = row
    return BackfillEntries(count, first_cached, last_cached)


def extend_backfill_entries(conn: psycopg.Connection[Any], ttl_days: int) -> int:
    """Give each of the backfill's entries ``ttl_days`` from when it was cached; count them."""
    result = conn.execute(_EXTEND_SQL, (ttl_days, _KEY_PREFIX, _BACKFILL_LIFETIME))
    return result.rowcount


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="extend the entries")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings()
    dsn = os.environ.get("DATABASE_URL") or settings.database_url
    ttl_days = settings.mb_cache_ttl_days

    with psycopg.connect(dsn) as conn:
        found = find_backfill_entries(conn)
        print(f"backfill cache entries with a 30-day lifetime: {found.count}")
        if found.first_cached is not None and found.last_cached is not None:
            print(f"  cached {found.first_cached:%Y-%m-%d %H:%M %Z}")
            print(f"      to {found.last_cached:%Y-%m-%d %H:%M %Z}")

        if not args.apply:
            print(f"\ndry run; pass --apply to extend them to {ttl_days} days")
            return
        changed = extend_backfill_entries(conn, ttl_days)
        conn.commit()
        print(f"\nextended {changed} entries to {ttl_days} days from when each was cached")


if __name__ == "__main__":
    main()

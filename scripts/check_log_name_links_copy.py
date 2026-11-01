"""Check migration 0044's copy, straight after it ran (artist model phase 1a, SPEC S13).

Usage: uv run python scripts/check_log_name_links_copy.py --dsn <a bench database's DSN>

Old and new must match one for one: every artist row of ``matches`` with a target maps to the
link of its (log name, artist) pair, the artist its target names by catalog id, else by MBID
(a stub of the MBID by now); and every link maps back to at least one such row. A pair several
rows held collapsed into one link: those rows count as collapsed. Run it on the gate's copy
after ``migrate``, before any task writes a link. Reads only, in one READ ONLY transaction;
like the gate tools it refuses a database without "bench" in its name. Exits 1 when the copy
does not match.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import gate_common
import psycopg
from psycopg.rows import dict_row

# Each old artist row with a target, and the link of the pair it maps to (NULL: none).
_OLD_ROWS_SQL = """
SELECT m.id::text AS match_id, l.id::text AS link_id
  FROM matches m
  LEFT JOIN LATERAL (
      SELECT c.id FROM artists c
       WHERE c.id = m.target_id OR c.mbid = m.target_id
       ORDER BY (c.id = m.target_id) DESC
       LIMIT 1
  ) a ON true
  LEFT JOIN log_name_links l ON l.broadcast_artist_id = m.artist_id AND l.artist_id = a.id
 WHERE m.artist_id IS NOT NULL
   AND COALESCE(m.target_id, '') <> ''
"""

_LINK_IDS_SQL = "SELECT id::text AS link_id FROM log_name_links"


@dataclass(frozen=True)
class CopyCheck:
    """How the old artist rows and the links match."""

    old_rows: int
    links: int
    collapsed: int
    """Old rows beyond the first of their pair."""
    unmapped_rows: tuple[str, ...]
    """Old rows (match ids) with no link for their pair."""
    links_without_rows: tuple[str, ...]
    """Links (ids) no old row maps to."""

    @property
    def ok(self) -> bool:
        return not self.unmapped_rows and not self.links_without_rows


def check(conn: psycopg.Connection[Any]) -> CopyCheck:
    """Compare the old artist rows of ``matches`` with ``log_name_links`` on *conn*."""
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(_OLD_ROWS_SQL).fetchall()
        link_ids = {row["link_id"] for row in cur.execute(_LINK_IDS_SQL).fetchall()}
    mapped = [row["link_id"] for row in rows if row["link_id"] is not None]
    return CopyCheck(
        old_rows=len(rows),
        links=len(link_ids),
        collapsed=len(mapped) - len(set(mapped)),
        unmapped_rows=tuple(sorted(row["match_id"] for row in rows if row["link_id"] is None)),
        links_without_rows=tuple(sorted(link_ids - set(mapped))),
    )


def render(report: CopyCheck) -> str:
    lines = [
        f"old artist rows: {report.old_rows}",
        f"links:           {report.links}",
        f"collapsed rows:  {report.collapsed}",
        f"rows with no link:  {len(report.unmapped_rows)}",
        *(f"  match {match_id}" for match_id in report.unmapped_rows),
        f"links with no row:  {len(report.links_without_rows)}",
        *(f"  link {link_id}" for link_id in report.links_without_rows),
        "one for one: " + ("yes" if report.ok else "NO"),
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", required=True, help="the gate's copy (a bench database)")
    args = parser.parse_args(argv)
    gate_common.require_bench(args.dsn)
    with psycopg.connect(args.dsn) as conn:
        conn.read_only = True
        try:
            report = check(conn)
        finally:
            conn.rollback()
    print(render(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())

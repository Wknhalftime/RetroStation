"""Fold missing library rows into the present copy of the same track.

Run: uv run python scripts/reconcile_missing_files.py            # dry run: report only
     uv run python scripts/reconcile_missing_files.py --apply    # fold the rows in

A file moved and retagged in one go (MusicBrainz Picard) left its old row
behind as MISSING, still holding its matches and song-master pick. Scans
now fold such rows in as they happen; this clears the backlog left before.
--apply runs the same reconciliation a scan does: each move in its own
transaction, so an interrupted run can simply be run again, then a re-pick
of masters left on missing files. Take a backup first: folded rows cannot be
restored without one.

Targets ``DATABASE_URL`` if set, else the app's configured database.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import psycopg
from psycopg.rows import dict_row

from backend.config import Settings
from backend.domain.library import MissingFilePlan
from backend.services.missing_file_reconciliation_service import plan_for_library
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.library_scan_tasks import reconcile_missing_after_scan


def _use_utf8_console() -> None:
    """Reconfigure stdout/stderr to UTF-8 so non-ASCII paths print on a cp1252 console."""
    # Must reconfigure before any other printing to prevent cp1252 Unicode crashes on Windows
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


def format_report(plan: MissingFilePlan, masters_to_repick: int) -> list[str]:
    total = len(plan.moves) + len(plan.ambiguous) + len(plan.unmatched)
    lines = [
        f"missing rows:           {total}",
        f"  to fold in:           {len(plan.moves)}",
        f"  ambiguous:            {len(plan.ambiguous)}",
        f"  no present copy:      {len(plan.unmatched)}",
        f"masters to re-pick:     {masters_to_repick}",
    ]
    crossing = [m for m in plan.moves if m.crosses_work]
    if crossing:
        lines.append(f"\nfolds into another work ({len(crossing)}):")
        lines += [f"  {m.missing_path}  ->  {m.successor_path}" for m in crossing]
    if plan.ambiguous:
        lines.append("\nambiguous (left alone):")
        lines += [f"  {path}" for path in plan.ambiguous]
    return lines


def apply_and_summarise(conn: psycopg.Connection[Any], factory: RepositoryFactory) -> str:
    """Run the scans' reconciliation and summarise what it did in one line."""
    result = reconcile_missing_after_scan(conn, factory)
    if result is None:
        # Autocommit: the folds made before the failure are kept; a re-run continues.
        return "reconciliation stopped early; see missing_reconciliation_failed in the log"
    return (
        f"folded {result.reconciled}, failed {result.failed}, "
        f"masters re-picked {result.masters_repicked} "
        "(failed folds are logged as missing_file_fold_failed)"
    )


def main() -> None:
    _use_utf8_console()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="fold the rows in")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL") or Settings().database_url
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        factory = RepositoryFactory(conn)
        plan = plan_for_library(factory.library_files)
        masters = len(factory.song_masters.list_work_ids_with_missing_master())
        print("\n".join(format_report(plan, masters)))
        if not args.apply:
            print("\ndry run: nothing changed. Re-run with --apply to fold the rows in.")
            return
        print("\n" + apply_and_summarise(conn, factory))


if __name__ == "__main__":
    main()

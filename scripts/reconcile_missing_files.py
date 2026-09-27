"""Fold missing library rows into the present copy of the same track.

Run: uv run python scripts/reconcile_missing_files.py            # dry run: report only
     uv run python scripts/reconcile_missing_files.py --apply    # fold the rows in

A file moved and retagged in one go (MusicBrainz Picard) left its old row
behind as MISSING, still holding its matches and song-master pick. Scans
now fold such rows in as they happen; this clears the backlog left before.
Each move runs in its own transaction, so an interrupted run can simply be
run again. Take a backup first: folded rows cannot be restored without one.

Targets ``DATABASE_URL`` if set, else the app's configured database.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

from backend.config import Settings
from backend.domain.library import MissingFilePlan
from backend.services.missing_file_reconciliation_service import (
    ReconciliationRepos,
    apply_missing_file_move,
    plan_for_library,
)
from backend.services.repository_factory import RepositoryFactory


def format_report(plan: MissingFilePlan) -> list[str]:
    total = len(plan.moves) + len(plan.ambiguous) + len(plan.unmatched)
    lines = [
        f"missing rows:           {total}",
        f"  to fold in:           {len(plan.moves)}",
        f"  ambiguous:            {len(plan.ambiguous)}",
        f"  no present copy:      {len(plan.unmatched)}",
    ]
    crossing = [m for m in plan.moves if m.crosses_work]
    if crossing:
        lines.append(f"\nfolds into another work ({len(crossing)}):")
        lines += [f"  {m.missing_path}  ->  {m.successor_path}" for m in crossing]
    if plan.ambiguous:
        lines.append("\nambiguous (left alone):")
        lines += [f"  {path}" for path in plan.ambiguous]
    return lines


def apply_plan(
    plan: MissingFilePlan,
    conn: psycopg.Connection[Any],
    repos: ReconciliationRepos,
) -> tuple[int, int]:
    """Apply each move in its own transaction. Returns (folded, skipped)."""
    folded = skipped = 0
    for move in plan.moves:
        try:
            with conn.transaction():
                apply_missing_file_move(move, repos)
        except psycopg.errors.ForeignKeyViolation:
            # The successor was deleted since planning; the next scan re-plans.
            skipped += 1
            continue
        folded += 1
    return folded, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="fold the rows in")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL") or Settings().database_url
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        factory = RepositoryFactory(conn)
        repos = ReconciliationRepos(
            files=factory.library_files,
            matches=factory.matches,
            works=factory.works,
            song_masters=factory.song_masters,
            format_overrides=factory.format_overrides,
        )
        plan = plan_for_library(repos.files)
        print("\n".join(format_report(plan)))
        if not args.apply:
            print("\ndry run: nothing changed. Re-run with --apply to fold the rows in.")
            return
        folded, skipped = apply_plan(plan, conn, repos)
        print(f"\nfolded {folded} rows; skipped {skipped} whose successor vanished.")


if __name__ == "__main__":
    main()

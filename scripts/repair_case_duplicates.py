"""Merge library rows that name one file in different case.

Run: uv run python scripts/repair_case_duplicates.py            # dry run: report only
     uv run python scripts/repair_case_duplicates.py --apply    # perform the repairs

Case-only renames that also retagged the file, or renamed its folder, used
to leave a second row under the old spelling, often still holding the
file's matches and song-master picks. For each group the row spelled as
the file is on disk is kept; the others' references move to it and they
are deleted. Groups whose file is gone, or whose on-disk spelling can't be
read, are listed and left alone.

Targets ``DATABASE_URL`` if set, else the app's configured database. Each
group is repaired in its own transaction, so an interrupted run can simply
be run again. Take a backup first: deleted rows cannot be restored without one.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

from backend.config import Settings
from backend.domain.library import CaseDuplicateRepair
from backend.services.case_duplicate_service import (
    apply_case_duplicate_repair,
    plan_case_duplicate_repairs,
)
from backend.services.repository_factory import RepositoryFactory

_SAMPLE_SIZE = 10


def _reference_counts(
    conn: psycopg.Connection[Any], stale_ids: list[str],
) -> dict[str, int]:
    row = conn.execute(
        """SELECT
             (SELECT count(*) FROM matches WHERE library_file_id = ANY(%(ids)s)) AS matches,
             (SELECT count(*) FROM song_masters
                WHERE preferred_file_id = ANY(%(ids)s)) AS song_masters,
             (SELECT count(*) FROM format_overrides
                WHERE preferred_file_id = ANY(%(ids)s)) AS format_overrides""",
        {"ids": stale_ids},
    ).fetchone()
    return dict(row) if row else {}


def _print_report(
    conn: psycopg.Connection[Any],
    repairs: list[CaseDuplicateRepair],
    unresolved: list[list[str]],
) -> None:
    stale_ids = [str(s) for r in repairs for s in r.stale_ids]
    print(f"duplicate groups:       {len(repairs) + len(unresolved)}")
    print(f"  repairable:           {len(repairs)}")
    print(f"  left alone:           {len(unresolved)}")
    print(f"stale rows to delete:   {len(stale_ids)}")
    for table, count in _reference_counts(conn, stale_ids).items():
        print(f"  {table + ' to move:':<21} {count}")
    print(f"\nfirst {_SAMPLE_SIZE} keepers:")
    for repair in repairs[:_SAMPLE_SIZE]:
        print(f"  {repair.keeper_path}")
    if unresolved:
        print("\nleft alone (file gone, or its spelling can't be read):")
        for paths in unresolved:
            print("  " + " | ".join(paths))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="perform the repairs")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL") or Settings().database_url
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        repos = RepositoryFactory(conn)
        repairs, unresolved = plan_case_duplicate_repairs(
            repos.library_files.get_case_duplicate_groups(),
        )
        _print_report(conn, repairs, unresolved)
        if not args.apply:
            print("\ndry run: nothing changed. Re-run with --apply to repair.")
            return

        repaired = failed = 0
        for repair in repairs:
            try:
                with conn.transaction():
                    apply_case_duplicate_repair(repair, repos.library_files)
            except psycopg.errors.ForeignKeyViolation:
                # The keeper was deleted since planning; its group is moot.
                failed += 1
                continue
            repaired += 1
        print(f"\nrepaired {repaired} groups; skipped {failed} whose keeper vanished.")


if __name__ == "__main__":
    main()

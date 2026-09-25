"""Merge duplicate local works (same artist, same normalized title).

Run: uv run python scripts/dedupe_works.py            # dry run: report only
     uv run python scripts/dedupe_works.py --apply    # perform the merges

Targets ``DATABASE_URL`` if set, else the app's configured database. Each
duplicate group is merged in its own transaction, so an interrupted run
leaves every group either fully merged or untouched and can simply be run
again. Take a backup first; merged-away works cannot be restored without one.
"""

from __future__ import annotations

import argparse
import os
from collections import Counter

import psycopg
from psycopg.rows import dict_row

from backend.config import Settings
from backend.domain.catalog import WorkFootprint, WorkMergePlan
from backend.services.repository_factory import RepositoryFactory
from backend.services.work_dedup_service import (
    MergeTargetNotFoundError,
    merge_work_group,
    plan_duplicate_merges,
)

_SAMPLE_SIZE = 15


def _print_report(plans: list[WorkMergePlan], footprints: list[WorkFootprint]) -> None:
    by_id = {f.id: f for f in footprints}
    sources = [by_id[s] for p in plans for s in p.source_ids]
    sizes = Counter(len(p.source_ids) + 1 for p in plans)
    print(f"local works scanned:      {len(footprints)}")
    print(f"duplicate groups:         {len(plans)}")
    print(f"works to merge away:      {len(sources)}")
    print(f"  of which have no files: {sum(1 for s in sources if s.file_count == 0)}")
    print(f"files to re-point:        {sum(s.file_count for s in sources)}")
    print(f"matches to re-point:      {sum(s.match_count for s in sources)}")
    print(f"group sizes:              {dict(sorted(sizes.items()))}")
    print(f"\nlargest {_SAMPLE_SIZE} groups (survivor first):")
    for plan in sorted(plans, key=lambda p: -len(p.source_ids))[:_SAMPLE_SIZE]:
        members = [by_id[plan.target_id], *(by_id[s] for s in plan.source_ids)]
        print("  " + " | ".join(f"{m.title!r} ({m.file_count}f)" for m in members))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="perform the merges")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL") or Settings().database_url
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        repos = RepositoryFactory(conn)
        footprints = repos.works.list_local_footprints()
        plans = plan_duplicate_merges(footprints)
        _print_report(plans, footprints)
        if not args.apply:
            print("\ndry run: nothing changed. Re-run with --apply to merge.")
            return

        merged = skipped = 0
        for index, plan in enumerate(plans, start=1):
            try:
                with conn.transaction():
                    merge_work_group(
                        plan,
                        work_repo=repos.works,
                        song_master_repo=repos.song_masters,
                        library_file_repo=repos.library_files,
                    )
            except MergeTargetNotFoundError:
                skipped += 1  # removed since planning; its group is moot
                continue
            merged += 1
            if index % 500 == 0:
                print(f"  {index}/{len(plans)} groups")
        print(f"\nmerged {merged} groups; skipped {skipped} whose survivor vanished.")


if __name__ == "__main__":
    main()

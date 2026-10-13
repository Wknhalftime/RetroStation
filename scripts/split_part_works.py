"""Move files out of local works whose part number they do not share.

Grouping used to put "Another Brick in the Wall, Part 1" and "..., Part 3"
on the work "..., Part II". This moves each such file to the work of its own
part (found or created as grouping does today); its matches move with it.
A work whose title names no part ("Shine On You Crazy Diamond" holding
Parts 1 and 2) is left alone.

Run: uv run python scripts/split_part_works.py            # dry run: report only
     uv run python scripts/split_part_works.py --apply    # move the files

Targets ``DATABASE_URL`` if set, else the app's configured database. Each work
is split in its own transaction, so an interrupted run can simply be run again.
"""

from __future__ import annotations

import argparse
import os

import psycopg
from psycopg.rows import dict_row

from backend.config import Settings
from backend.domain.catalog import PartSplitPlan
from backend.repositories.library_files import LibraryFileRepository
from backend.services.part_split_service import (
    PartSplitRepos,
    plan_part_splits,
    split_misplaced_parts,
)
from backend.services.repository_factory import RepositoryFactory


def _print_report(plans: list[PartSplitPlan], files: LibraryFileRepository) -> None:
    print(f"works to split:  {len(plans)}")
    print(f"files to move:   {sum(len(p.file_ids) for p in plans)}")
    for plan in plans:
        titles = sorted(f.audio.track_title or "" for f in files.get_by_ids(list(plan.file_ids)))
        print(f"  {plan.work_title!r}: {' | '.join(repr(t) for t in titles)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="move the files")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL") or Settings().database_url
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        factory = RepositoryFactory(conn)
        repos = PartSplitRepos(
            artists=factory.artists,
            works=factory.works,
            files=factory.library_files,
            song_masters=factory.song_masters,
            matches=factory.matches,
        )
        plans = plan_part_splits(repos.works, repos.files)
        _print_report(plans, repos.files)
        if not args.apply:
            print("\ndry run: nothing changed. Re-run with --apply to move the files.")
            return

        for plan in plans:
            with conn.transaction():
                split_misplaced_parts(plan, repos)
        print(f"\nsplit {len(plans)} works.")


if __name__ == "__main__":
    main()

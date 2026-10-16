"""Settle works whose song master points at a file on another work.

Moves made before the work a file left was settled (enrichment before #110,
case-duplicate repairs before this script's PR) re-pointed or kept a work's
master on a file that went to another work, and left the work behind. Each
such work is settled as a move settles the work a file leaves today: deleted
once nothing references it, else its master is re-picked from its own present
files (removed when it has none).

Run: uv run python scripts/repair_misplaced_masters.py            # dry run: report only
     uv run python scripts/repair_misplaced_masters.py --apply    # settle the works

Targets ``DATABASE_URL`` if set, else the app's configured database. All works
are settled in one transaction. Take a backup first: deleted works cannot be
restored without one.
"""

from __future__ import annotations

import argparse
import os

import psycopg
from psycopg.rows import dict_row

from backend.config import Settings
from backend.services.repository_factory import RepositoryFactory
from backend.services.work_move_service import WorkMoveRepos, settle_misplaced_masters


def _title(repos: WorkMoveRepos, work_id: str | None) -> str:
    work = repos.works.get_by_id(work_id) if work_id is not None else None
    return repr(work.title) if work is not None else "(no work)"


def _print_report(repos: WorkMoveRepos, work_ids: list[str]) -> None:
    print(f"masters off their work: {len(work_ids)}")
    for work_id in work_ids:
        master = repos.song_masters.get_by_work(work_id)
        file = repos.files.get_by_id(master.preferred_file_id) if master else None
        file_work = _title(repos, file.work_id if file else None)
        method = master.selection_method.value if master else "?"
        print(f"  {_title(repos, work_id)} ({method}) -> file on {file_work}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="settle the works")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL") or Settings().database_url
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        factory = RepositoryFactory(conn)
        repos = WorkMoveRepos(
            files=factory.library_files,
            works=factory.works,
            song_masters=factory.song_masters,
            matches=factory.matches,
        )
        _print_report(repos, repos.song_masters.list_work_ids_with_master_off_work())
        if not args.apply:
            print("\ndry run: nothing changed. Re-run with --apply to settle the works.")
            return

        with conn.transaction():
            settled = settle_misplaced_masters(repos)
            deleted = sum(1 for w in settled if repos.works.get_by_id(w) is None)
        print(f"\nsettled {len(settled)} works: {deleted} deleted, {len(settled) - deleted} kept.")


if __name__ == "__main__":
    main()

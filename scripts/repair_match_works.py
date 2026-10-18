"""Move matches whose work is not their file's work onto the file's work.

``matches.work_id`` mirrors the matched file's work. Grouping that gave a file
its first work left matches made before it with none, and some older moves
left them on the work the file moved away from. Each such file's matches take
its work, and each work they left is settled as a move settles the work a file
leaves: deleted once nothing references it, else its master is re-picked.

Run: uv run python scripts/repair_match_works.py            # dry run: report only
     uv run python scripts/repair_match_works.py --apply    # realign the matches

Targets ``DATABASE_URL`` if set, else the app's configured database. Everything
is done in one transaction. Take a backup first: deleted works cannot be
restored without one.
"""

from __future__ import annotations

import argparse
import os

import psycopg
from psycopg.rows import dict_row

from backend.config import Settings
from backend.domain.matching import MatchOffFileWork
from backend.services.repository_factory import RepositoryFactory
from backend.services.work_move_service import WorkMoveRepos, realign_match_works


def _title(repos: WorkMoveRepos, work_id: str | None) -> str:
    work = repos.works.get_by_id(work_id) if work_id is not None else None
    return repr(work.title) if work is not None else "(no work)"


def _print_report(repos: WorkMoveRepos, off_work: list[MatchOffFileWork]) -> None:
    print(f"files with matches off their work: {len(off_work)}")
    for drift in off_work:
        file = repos.files.get_by_id(drift.library_file_id)
        name = file.file_path if file is not None else str(drift.library_file_id)
        print(f"  {name}")
        print(f"    match on {_title(repos, drift.match_work_id)}")
        print(f"    file on  {_title(repos, drift.file_work_id)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="realign the matches")
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
        _print_report(repos, repos.matches.list_off_file_work())
        if not args.apply:
            print("\ndry run: nothing changed. Re-run with --apply to realign the matches.")
            return

        with conn.transaction():
            realigned = realign_match_works(repos)
            left = {d.match_work_id for d in realigned if d.match_work_id is not None}
            deleted = sum(1 for w in left if repos.works.get_by_id(w) is None)
        print(f"\nrealigned {len(realigned)}; works left: {len(left)}, {deleted} deleted.")


if __name__ == "__main__":
    main()

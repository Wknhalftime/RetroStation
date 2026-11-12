"""Credit picks' Apply on a gate copy (performances phase 3a PR 3, SPEC S9; the gate's --apply).

Usage:
    python scripts/apply_credit_picks.py --dsn <copy> [--skip <file>]

Applies every undecided ``credit`` proposal on the copy, by Apply's rules and through
``SongMatchService``: the performance is stored on the line's best match (given a preferred copy
first) and the row is marked applied. The skip file names lines, one ``track_identities`` id a
row (blank rows and ``#`` comments ignored); their rows are marked skipped. Version lines, ties,
decided rows and stale rows are left as they are, so a second run applies nothing. Like every
gate tool it refuses a database without "bench" in its name before touching anything (S10).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_common  # noqa: E402

if __name__ == "__main__":
    gate_common.guard_from_argv(sys.argv[1:])  # S10: before the backend imports below

from backend.services.credit_pick_service import apply_on_copy  # noqa: E402
from backend.services.repository_factory import (  # noqa: E402
    RepositoryFactory,
    credit_pick_repos,
)


def read_skips(path: Path | None) -> set[UUID]:
    """The lines named in the skip file."""
    if path is None:
        return set()
    lines: set[UUID] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        text = raw.split("#", 1)[0].strip()
        if text:
            lines.add(UUID(text))
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dsn", required=True, help="the gate copy (a bench database)")
    parser.add_argument("--skip", type=Path, help="lines to skip, one id a row")
    args = parser.parse_args(argv)
    gate_common.set_guards(args.dsn)
    skips = read_skips(args.skip)
    with psycopg.connect(args.dsn, row_factory=dict_row) as conn:
        result = apply_on_copy(skips, credit_pick_repos(RepositoryFactory(conn)))
        conn.commit()
    print(f"applied {result.applied}, skipped {result.skipped}, stale {len(result.stale)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

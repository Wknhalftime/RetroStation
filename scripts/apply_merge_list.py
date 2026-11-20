"""The merge list's Apply on a gate copy (work merges phase 3b PR 4, SPEC S11, D26; the gate's
--apply).

Usage:
    python scripts/apply_merge_list.py --dsn <copy> [--skip <file>]

Plans every group as the merge list does and applies each one as Apply does: through the guard,
reason "merge list", with no approvals, committing after each group. A group whose merge would
change what a pin can't hold waits and is counted. The skip file names works, one id a row
(blank rows and ``#`` comments ignored); a group holding any of them is skipped. Writes, to
``GATE_RUN_DIR``, ``work_map.json`` (each merged-away work -> the work it merged into) and
``merge_list_allow.txt``, the allowlist of the differences the merges make. Like every gate tool
it refuses a database without "bench" in its name before touching anything (S10).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent))
import airplay_snapshot  # noqa: E402
import gate_common  # noqa: E402

if __name__ == "__main__":
    gate_common.guard_from_argv(sys.argv[1:])  # S10: before the backend imports below

from backend.domain.merge_list import GroupOutcome, GroupResult, SeenGroup  # noqa: E402
from backend.domain.work_merge_planner import CreditsNotReadyError  # noqa: E402
from backend.services import merge_list_service  # noqa: E402
from backend.services.merge_plan_service import plan  # noqa: E402
from backend.services.repository_factory import (  # noqa: E402
    RepositoryFactory,
    merge_list_repos,
)

WORK_MAP = "work_map.json"
ALLOWLIST = "merge_list_allow.txt"

type Conn = psycopg.Connection[Any]


def read_skips(path: Path | None) -> set[str]:
    """The works named in the skip file."""
    if path is None:
        return set()
    works: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        text = raw.split("#", 1)[0].strip()
        if text:
            works.add(text)
    return works


def _held(conn: Conn) -> dict[str, tuple[str | None, str | None]]:
    """Each line holding a pin or a stored performance on a match: (pinned file, performance)."""
    rows = conn.execute(
        """SELECT identity_id, pinned_file_id, performance_id FROM matches
           WHERE pinned_file_id IS NOT NULL OR performance_id IS NOT NULL"""
    ).fetchall()
    held: dict[str, tuple[str | None, str | None]] = {}
    for r in rows:
        held[str(r["identity_id"])] = (
            None if r["pinned_file_id"] is None else str(r["pinned_file_id"]),
            None if r["performance_id"] is None else str(r["performance_id"]),
        )
    return held


def _artists(conn: Conn, work_ids: Sequence[str]) -> dict[str, set[str]]:
    rows = conn.execute(
        "SELECT id, artist_id FROM works WHERE id = ANY(%s)", (list(work_ids),)
    ).fetchall()
    found: dict[str, set[str]] = {}
    for r in rows:
        found.setdefault(str(r["id"]), set()).add(str(r["artist_id"]))
    return found


def _allowlist(
    conn: Conn,
    merged: Sequence[GroupResult],
    held_before: dict[str, tuple[str | None, str | None]],
    artists: dict[str, set[str]],
) -> list[tuple[str, str, str]]:
    """The differences the merges make, as (section, key, reason)."""
    lines: list[tuple[str, str, str]] = []
    held_after = _held(conn)
    moved = sorted(
        i for i in set(held_before) | set(held_after) if held_before.get(i) != held_after.get(i)
    )
    if moved:
        rows = conn.execute(
            """SELECT station_id, identity_id FROM station_identity_plays
               WHERE identity_id = ANY(%s::uuid[]) AND play_count > 0
               ORDER BY station_id, identity_id""",
            (moved,),
        ).fetchall()
        lines += [
            (
                "work-source",
                f"{r['station_id']}/{r['identity_id']}",
                "held by the merge (pin or stored performance)",
            )
            for r in rows
        ]
    for result in merged:
        target = result.target_id
        lines.append(("masters", target, "the merged work's master"))
        overrides = conn.execute(
            "SELECT * FROM format_overrides WHERE work_id = %s", (target,)
        ).fetchall()
        lines += [
            ("overrides", key, "an override of the merged work")
            for key in sorted(airplay_snapshot.override_key(row) for row in overrides)
        ]
        lines.append(("work-detail", target, "the merged work"))
        # D35: a sampled merged-away id now answers its target's detail (PR 3's redirect).
        lines += [
            ("work-detail", source, "merged into the work it now redirects to")
            for source in result.source_ids
        ]
        for artist in sorted(
            set().union(*(artists.get(w, set()) for w in (target, *result.source_ids)))
        ):
            lines.append(("artist-detail", artist, "an artist of a merged work"))
            lines.append(("artist-list", artist, "an artist of a merged work"))
    return lines


def _write(run_dir: Path, work_map: dict[str, str], allow: list[tuple[str, str, str]]) -> None:
    (run_dir / WORK_MAP).write_text(
        json.dumps(work_map, indent=1, sort_keys=True), encoding="utf-8"
    )
    text = "".join(f"{section}\t{key}\t{reason}\n" for section, key, reason in allow)
    (run_dir / ALLOWLIST).write_text(
        "# Written by scripts/apply_merge_list.py (3b S11)\n" + text, encoding="utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dsn", required=True, help="the gate copy (a bench database)")
    parser.add_argument("--skip", type=Path, help="works to skip, one id a row")
    args = parser.parse_args(argv)
    gate_common.set_guards(args.dsn)
    skips = read_skips(args.skip)
    with psycopg.connect(args.dsn, row_factory=dict_row) as conn:
        repos = merge_list_repos(RepositoryFactory(conn))
        try:
            planned = plan(repos.merge_list, repos.settings).groups
        except CreditsNotReadyError as refused:
            print(f"apply_merge_list: {refused}")
            return 1
        chosen = [g for g in planned if not ({g.target_id, *g.source_ids} & skips)]
        held_before = _held(conn)
        artists = _artists(conn, [w for g in chosen for w in (g.target_id, *g.source_ids)])
        conn.commit()
        report = merge_list_service.apply(
            [SeenGroup(g.target_id, g.source_ids, frozenset()) for g in chosen], repos, conn.commit
        )
        merged = [r for r in report.results if r.outcome is GroupOutcome.MERGED]
        work_map = {s: r.target_id for r in merged for s in r.source_ids}
        allow = _allowlist(conn, merged, held_before, artists)
        conn.commit()
    run_dir = os.environ.get("GATE_RUN_DIR")
    if run_dir:
        _write(Path(run_dir), work_map, allow)
    print(
        f"merged {report.count(GroupOutcome.MERGED)}, waiting {report.count(GroupOutcome.WAITING)},"
        f" failed {report.count(GroupOutcome.FAILED)}, skipped {len(planned) - len(chosen)},"
        f" already merged {report.count(GroupOutcome.ALREADY_MERGED)},"
        f" changed {report.count(GroupOutcome.CHANGED)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

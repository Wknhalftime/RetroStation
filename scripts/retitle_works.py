"""Re-derive local work titles from their files' tags.

A local work is titled once, when grouping creates it, from the first
file's tag run through the version stripper. When the stripper is fixed
(nested parentheses, "[Language]") the works it mis-titled keep their old
title. This re-derives each local work's title from all of its files and
changes it only when the current title is not what any file gives, so
casing and punctuation variants among files are left alone.

Run: uv run python scripts/retitle_works.py                 # dry run: report only
     uv run python scripts/retitle_works.py --artist Aerosmith
     uv run python scripts/retitle_works.py --apply         # write the new titles

Targets ``DATABASE_URL`` if set, else the app's configured database.
After applying, run scripts/dedupe_works.py: a corrected title may now
match an existing work with the same normalized title.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import Settings  # noqa: E402
from backend.services.normalization import extract_version_info  # noqa: E402


def plan_retitle(work_title: str, track_titles: Sequence[str]) -> str | None:
    """The title the work's files give it, or None when the current one is fine.

    Each file's tag is stripped of version tags; the most common base title
    wins. The current title stands when it already matches one of the
    files' base titles (ignoring case), so a work is not renamed over a
    spelling difference between its own files.
    """
    bases = Counter(
        extract_version_info(title)[0].strip() for title in track_titles if title and title.strip()
    )
    bases.pop("", None)
    if not bases:
        return None
    if work_title.strip().lower() in {b.lower() for b in bases}:
        return None
    winner, _count = bases.most_common(1)[0]
    return winner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the new titles")
    parser.add_argument("--artist", help="only works by this artist (case-insensitive name)")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    dsn = os.environ.get("DATABASE_URL") or Settings().database_url

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        params: list[object] = []
        artist_clause = ""
        if args.artist:
            artist_clause = "AND a.normalized_name = %s"
            params.append(args.artist.lower())
        rows = conn.execute(
            f"""
            SELECT w.id, w.title, a.name AS artist,
                   array_agg(lf.track_title ORDER BY lf.file_path) AS track_titles
              FROM works w
              JOIN artists a ON a.id = w.artist_id
              JOIN library_files lf ON lf.work_id = w.id
             WHERE w.origin = 'local' {artist_clause}
             GROUP BY w.id, w.title, a.name
             ORDER BY a.name, w.title
            """,
            params,
        ).fetchall()

        changes = [
            (row["id"], row["artist"], row["title"], new_title)
            for row in rows
            if (new_title := plan_retitle(row["title"], row["track_titles"])) is not None
        ]
        print(f"local works with files: {len(rows)}")
        print(f"titles to change:       {len(changes)}")
        for _id, artist, old, new in changes:
            print(f"  {artist}: {old!r} -> {new!r}")

        if not args.apply:
            print("\ndry run; pass --apply to write")
            return
        for work_id, _artist, _old, new in changes:
            conn.execute("UPDATE works SET title = %s WHERE id = %s", (new, work_id))
        conn.commit()
        print(f"\nupdated {len(changes)} work titles")


if __name__ == "__main__":
    main()

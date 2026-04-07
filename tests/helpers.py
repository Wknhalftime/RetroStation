"""Shared test helpers for grouping invariant assertions."""
from __future__ import annotations

from typing import Any

import psycopg


def assert_grouping_invariants(conn: psycopg.Connection[Any]) -> None:
    """Assert all 6 grouping invariants hold.

    Run after every merge/split/reassign/enrichment test.
    """

    def count(sql: str) -> int:
        row = conn.execute(f"SELECT count(*) AS cnt FROM ({sql}) sub").fetchone()
        return row["cnt"] if row else 0

    # 1. No dangling library_files.work_id
    assert count(
        "SELECT 1 FROM library_files lf LEFT JOIN works w ON lf.work_id = w.id "
        "WHERE lf.work_id IS NOT NULL AND w.id IS NULL"
    ) == 0, "Invariant 1 violated: dangling library_files.work_id"

    # 2. Every work with files has exactly one song_master
    assert count(
        "SELECT w.id FROM works w "
        "JOIN library_files lf ON lf.work_id = w.id "
        "LEFT JOIN song_masters sm ON sm.work_id = w.id "
        "WHERE sm.id IS NULL "
        "GROUP BY w.id"
    ) == 0, "Invariant 2 violated: work with files but no song_master"

    # 3. No dangling format_overrides
    assert count(
        "SELECT 1 FROM format_overrides fo LEFT JOIN works w ON fo.work_id = w.id "
        "WHERE w.id IS NULL"
    ) == 0, "Invariant 3 violated: dangling format_overrides.work_id"

    # 4. No dangling recordings.work_id
    assert count(
        "SELECT 1 FROM recordings r LEFT JOIN works w ON r.work_id = w.id "
        "WHERE r.work_id IS NOT NULL AND w.id IS NULL"
    ) == 0, "Invariant 4 violated: dangling recordings.work_id"

    # 5. No dangling matches targeting deleted works
    assert count(
        "SELECT 1 FROM matches m LEFT JOIN works w ON m.target_id = w.id "
        "WHERE m.target_type = 'WORK' AND w.id IS NULL"
    ) == 0, "Invariant 5 violated: dangling matches targeting deleted work"

    # 6. No orphaned local artists
    assert count(
        "SELECT 1 FROM artists a "
        "LEFT JOIN works w ON w.artist_id = a.id "
        "WHERE a.origin = 'local' AND w.id IS NULL"
    ) == 0, "Invariant 6 violated: orphaned local artist"

"""The route's coverage connection is read-only on PostgreSQL (PR G2 final review T7-b; not
locked).

Requirements: PG7, I7: the coverage is counted on its own bounded, read-only connection; the
route's connection (``BoundedCueCoverageRepository``) carries ``COVERAGE_READ_BOUNDS`` and
``default_transaction_read_only``, as the cue run's count does.
"""

from __future__ import annotations

import psycopg
import pytest

from backend.db.repositories.stream_cue_coverage import (
    COVERAGE_READ_BOUNDS,
    BoundedCueCoverageRepository,
)
from backend.db.stream_reads import bounded_connection


def test_the_routes_coverage_connection_is_read_only(migrated_db: str) -> None:
    with bounded_connection(
        migrated_db, COVERAGE_READ_BOUNDS, autocommit=True, read_only=True
    ) as conn:
        shown = conn.execute("SHOW default_transaction_read_only").fetchone()
        timeout = conn.execute("SHOW statement_timeout").fetchone()
        assert shown is not None and shown["default_transaction_read_only"] == "on"
        assert timeout is not None and timeout["statement_timeout"] == "10s"
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("UPDATE progress_tracking SET status = status")
    counted = BoundedCueCoverageRepository(migrated_db, COVERAGE_READ_BOUNDS).coverage()
    assert counted.analysable == 0

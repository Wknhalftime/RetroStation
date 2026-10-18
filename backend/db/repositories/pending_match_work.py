"""How much matching work is pending, for the matching run's progress row (spec 2026-10-17).

Counted on a connection of its own (``connect``), never a matching stage's transaction: a
count that fails or times out there would abort the stage and roll back its matching.
"""

from __future__ import annotations

from typing import Any

import psycopg

from backend.db.progress_writer import Connect
from backend.db.telemetry_connection import TelemetryConnection
from backend.repositories.pending_match_work import PendingMatchWork

COUNT_OPTIONS = "-c lock_timeout=2000 -c statement_timeout=10000"
"""The count connection's startup options: a scan of two broadcast tables with no index of
its own on ``match_status``, bounded as the cue coverage counts are (10 s, 2 s on a lock), so
a cold disk does not lose a stage its start count."""

_COUNT_SQL = """SELECT (SELECT count(*) FROM broadcast_artists WHERE match_status = 'pending')
                     + (SELECT count(*) FROM track_identities WHERE match_status = 'pending')
                    AS pending"""


def _count(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute(_COUNT_SQL).fetchone()
    return int(row["pending"]) if row is not None else 0


class PgPendingMatchWork(PendingMatchWork):
    def __init__(self, connect: Connect) -> None:
        self._db = TelemetryConnection(connect, name="pending_match_work")

    def count_pending(self) -> int | None:
        return self._db.run(_count)

    def close(self) -> None:
        self._db.close()

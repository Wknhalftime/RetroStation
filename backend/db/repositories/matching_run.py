"""The matching run's progress row in ``progress_tracking`` (spec 2026-10-17).

Each write is one statement, with the counts worked out in SQL from the row's own
``progress_data``, so two writes can never interleave a read and a write. Only a RUNNING row,
or one the ``/ws`` reaper flipped to FAILED, is written; a write brings the latter back
(``status = 'running'``, ``completed_at`` cleared, as ``touch_running`` does). A COMPLETED row
and an unknown id match nothing.
"""

from __future__ import annotations

import json
from typing import Any

from backend.db.progress_writer import Connect
from backend.db.telemetry_connection import TelemetryConnection
from backend.domain.enums import TaskStatus, TaskType
from backend.repositories.matching_run import MatchingRunRepository

STAGES_FAILED_WARNING = "matching_stages_failed"

_OPEN_SQL = """INSERT INTO progress_tracking
   (task_id, task_type, status, progress_data, started_at, updated_at, completed_at)
   VALUES (%s, %s, %s, %s, now(), now(), NULL)"""

# ``new_total`` = max(total, processed + pending); ``processed`` = new_total - pending. A
# NULL pending keeps both.
_COUNTED_CTE = """WITH cur AS (
    SELECT task_id,
           (progress_data->>'total')::int AS total,
           (progress_data->>'processed')::int AS processed,
           (progress_data->>'stages_left')::int AS stages_left,
           (progress_data->>'stages_failed')::int AS stages_failed
      FROM progress_tracking
     WHERE task_id = %(run_id)s AND status IN ('running', 'failed')
), c AS (
    SELECT task_id, stages_left, stages_failed,
           CASE WHEN %(pending)s::int IS NULL THEN total
                ELSE GREATEST(total, processed + %(pending)s::int) END AS total,
           CASE WHEN %(pending)s::int IS NULL THEN processed
                ELSE GREATEST(total, processed + %(pending)s::int) - %(pending)s::int
           END AS processed
      FROM cur
)"""

_REFRESH_SQL = (
    _COUNTED_CTE
    + """
UPDATE progress_tracking AS p
   SET progress_data = p.progress_data
           || jsonb_build_object('total', c.total, 'processed', c.processed),
       status = 'running',
       updated_at = now(),
       completed_at = NULL
  FROM c
 WHERE p.task_id = c.task_id"""
)

_FINISH_STAGE_SQL = (
    _COUNTED_CTE
    + """, f AS (
    SELECT task_id, total, processed,
           stages_left - 1 AS stages_left,
           stages_failed + %(failed)s AS stages_failed
      FROM c
)
UPDATE progress_tracking AS p
   SET progress_data = p.progress_data
           || jsonb_build_object(
                  'total', f.total,
                  'processed', f.processed,
                  'stages_left', f.stages_left,
                  'stages_failed', f.stages_failed)
           || CASE WHEN f.stages_left <= 0 AND f.stages_failed > 0
                   THEN jsonb_build_object('warning', %(warning)s::text)
                   ELSE '{}'::jsonb END,
       status = CASE WHEN f.stages_left <= 0 THEN 'completed' ELSE 'running' END,
       updated_at = now(),
       completed_at = CASE WHEN f.stages_left <= 0 THEN now() END
  FROM f
 WHERE p.task_id = f.task_id"""
)


class PgMatchingRunRepository(MatchingRunRepository):
    """The run row on a connection of its own (``connect``), opened when first used."""

    def __init__(self, connect: Connect) -> None:
        self._db = TelemetryConnection(connect, name="matching_run")

    def open(self, run_id: str, *, total: int, stages: int) -> None:
        data = {"processed": 0, "total": total, "stages_left": stages, "stages_failed": 0}
        params = (run_id, TaskType.MATCHING.value, TaskStatus.RUNNING.value, json.dumps(data))
        self._db.run(lambda conn: conn.execute(_OPEN_SQL, params))

    def refresh(self, run_id: str, *, pending: int) -> None:
        params = {"run_id": run_id, "pending": pending}
        self._db.run(lambda conn: conn.execute(_REFRESH_SQL, params))

    def finish_stage(self, run_id: str, *, pending: int | None, failed: bool) -> None:
        params: dict[str, Any] = {
            "run_id": run_id,
            "pending": pending,
            "failed": 1 if failed else 0,
            "warning": STAGES_FAILED_WARNING,
        }
        self._db.run(lambda conn: conn.execute(_FINISH_STAGE_SQL, params))

    def close(self) -> None:
        self._db.close()

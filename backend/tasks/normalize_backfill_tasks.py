"""Background task to populate normalized fields for existing library files.

Runs after migration 0011 to backfill artist_name, normalized_artist_name,
and normalized_title from raw_metadata JSONB. Processes in batches of 500.
"""

from __future__ import annotations

from typing import Any

import psycopg
import psycopg.rows
import structlog

from backend.domain.enums import LogCategory, TaskType
from backend.services.normalization import normalize_artist, normalize_title
from backend.tasks._error_boundary import task_failure_telemetry
from backend.tasks.huey_app import huey

logger = structlog.get_logger()

BATCH_SIZE = 500


def _coerce_str(val: Any) -> str | None:
    if isinstance(val, list):
        return str(val[0]) if val else None
    return str(val) if val else None


def _extract_artist_from_metadata(meta: dict[str, Any]) -> str | None:
    """Extract artist name from raw_metadata JSONB (handles ID3 + Vorbis)."""
    for key in ("artist", "TPE1", "albumartist", "TPE2"):
        val = meta.get(key)
        if val:
            return _coerce_str(val)
    return None


def _extract_title_from_metadata(meta: dict[str, Any]) -> str | None:
    """Extract title from raw_metadata JSONB (handles ID3 + Vorbis)."""
    for key in ("title", "TIT2"):
        val = meta.get(key)
        if val:
            return _coerce_str(val)
    return None


@huey.task()  # type: ignore[untyped-decorator]
def normalize_backfill_task(db_url: str) -> None:
    """Populate normalized fields for files missing them.

    Wraps the batch loop in `task_failure_telemetry` so any exception (a
    DB outage mid-batch, a malformed raw_metadata JSONB) surfaces as a
    FAILED TaskProgress row + ERROR SystemLog entry before re-raising to
    Huey. Uses `TaskType.LIBRARY_ENRICHMENT` because the task enriches
    library_files rows with normalized fields. `database_url=db_url` so
    failure telemetry lands in the same DB being backfilled (not in
    settings.database_url, which may differ when the task is invoked
    with an explicit DSN).

    Pagination: uses `id > last_id` cursor bounds rather than the
    `normalized_artist_name IS NULL` predicate alone. A row with no
    extractable artist in `raw_metadata` stays NULL after UPDATE and
    would otherwise be re-fetched indefinitely; the cursor guarantees
    forward progress regardless of how individual rows respond to the
    update.
    """
    with task_failure_telemetry(
        TaskType.LIBRARY_ENRICHMENT, LogCategory.ENRICHMENT,
        database_url=db_url,
    ) as task_id:
        total = 0
        last_id: str | None = None
        with psycopg.connect(db_url, row_factory=psycopg.rows.dict_row) as conn:
            while True:
                if last_id is None:
                    rows = conn.execute(
                        """SELECT id, raw_metadata
                           FROM library_files
                           WHERE normalized_artist_name IS NULL
                             AND raw_metadata IS NOT NULL
                           ORDER BY id
                           LIMIT %s""",
                        (BATCH_SIZE,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT id, raw_metadata
                           FROM library_files
                           WHERE normalized_artist_name IS NULL
                             AND raw_metadata IS NOT NULL
                             AND id > %s
                           ORDER BY id
                           LIMIT %s""",
                        (last_id, BATCH_SIZE),
                    ).fetchall()

                if not rows:
                    break

                for row in rows:
                    meta = row["raw_metadata"]
                    artist = _extract_artist_from_metadata(meta)
                    title = _extract_title_from_metadata(meta)
                    conn.execute(
                        """UPDATE library_files
                           SET artist_name = %s,
                               normalized_artist_name = %s,
                               normalized_title = %s
                           WHERE id = %s""",
                        (
                            artist,
                            normalize_artist(artist) if artist else None,
                            normalize_title(title) if title else None,
                            row["id"],
                        ),
                    )
                conn.commit()
                total += len(rows)
                last_id = rows[-1]["id"]
                logger.info(
                    "backfill_progress", processed=total, task_id=task_id,
                )

        logger.info("backfill_complete", total=total, task_id=task_id)

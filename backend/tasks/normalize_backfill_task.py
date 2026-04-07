"""Background task to populate normalized fields for existing library files.

Runs after migration 0011 to backfill artist_name, normalized_artist_name,
and normalized_title from raw_metadata JSONB. Processes in batches of 500.
"""

from __future__ import annotations

from typing import Any

import psycopg
import psycopg.rows
import structlog

from backend.services.normalization import normalize_artist, normalize_title
from backend.tasks.huey_app import huey

logger = structlog.get_logger()

BATCH_SIZE = 500


def _extract_artist_from_metadata(meta: dict[str, Any]) -> str | None:
    """Extract artist name from raw_metadata JSONB (handles ID3 + Vorbis)."""
    for key in ("artist", "TPE1", "albumartist", "TPE2"):
        val = meta.get(key)
        if val:
            return val[0] if isinstance(val, list) else val
    return None


def _extract_title_from_metadata(meta: dict[str, Any]) -> str | None:
    """Extract title from raw_metadata JSONB (handles ID3 + Vorbis)."""
    for key in ("title", "TIT2"):
        val = meta.get(key)
        if val:
            return val[0] if isinstance(val, list) else val
    return None


@huey.task()  # type: ignore[untyped-decorator]
def normalize_backfill_task(db_url: str) -> None:
    """Populate normalized fields for files missing them."""
    total = 0
    with psycopg.connect(db_url, row_factory=psycopg.rows.dict_row) as conn:
        while True:
            rows = conn.execute(
                """SELECT id, raw_metadata
                   FROM library_files
                   WHERE normalized_artist_name IS NULL
                     AND raw_metadata IS NOT NULL
                   LIMIT %s""",
                (BATCH_SIZE,),
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
            logger.info("backfill_progress", processed=total)

    logger.info("backfill_complete", total=total)

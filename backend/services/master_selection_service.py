from __future__ import annotations

import structlog

from backend.repositories.song_masters import SongMasterRepository

logger = structlog.get_logger()

# Scoring constants per spec Section 5.4
RELEASE_STATUS_SCORE = {"promotion": 100, "official": 0}
RELEASE_TYPE_SCORE = {
    "album": 80, "ep": 70, "single": 60,
    "compilation": 40, "live": 30, "other": 20,
}
FORMAT_BONUS = {"flac": 10, "aac": 6, "ogg": 6, "mp3": 3}


def recalculate(
    work_ids: list[str],
    song_master_repo: SongMasterRepository,
) -> None:
    """Recalculate song masters for the given work IDs.

    Skips any work with selection_method='manual'.
    With no library files (Phase 1), this is a no-op.
    """
    if not work_ids:
        return

    logger.info("master_selection_recalculate", work_ids=len(work_ids), note="no-op in Phase 1")

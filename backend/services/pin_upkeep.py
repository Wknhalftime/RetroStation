"""Merge pins kept right by the paths that move or delete a file (phase 3b PR 3; SPEC D4, D14,
D23; design C72; AUD-R079).

A pin stands in for its line's work's master only, so a pinned file that leaves its work (a
split, a reassign, enrichment's move, a fold onto another work) or is deleted (Missing files'
delete, the purge) releases its pins first, logged; the line then takes the normal path. A fold
onto a copy on the same work re-points them instead (``library_files.merge_into``).
"""

from __future__ import annotations

from uuid import UUID

import structlog

from backend.repositories.matches import MatchRepository
from backend.services.song_match_service import SongMatchService

logger = structlog.get_logger()


def release_pins(file_id: UUID, matches: MatchRepository, *, why: str) -> int:
    """Release every pin on *file_id*, logged when there was one; how many."""
    released = SongMatchService(matches).release_pins(file_id)
    if released:
        logger.info("pins_released", file_id=str(file_id), lines=released, reason=why)
    return released

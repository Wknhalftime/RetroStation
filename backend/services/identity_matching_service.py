from __future__ import annotations

from uuid import UUID

import structlog

from backend.domain.enums import MatchStatus, MatchTier
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.log_identities import LogIdentityRepository
from backend.repositories.matches import MatchRepository

logger = structlog.get_logger()


def match_identities_for_playlist(
    playlist_id: UUID,
    log_identity_repo: LogIdentityRepository,
    match_repo: MatchRepository,
    library_file_repo: LibraryFileRepository,
) -> None:
    """Run identity matching for all pending identities in this playlist.

    With no library files (Phase 1), all identities with resolved artists
    are marked NEEDS_REVIEW.
    """
    pending = log_identity_repo.get_pending_for_playlist(playlist_id)

    if not pending:
        logger.info("no_pending_identities", playlist_id=str(playlist_id))
        return

    for identity in pending:
        log_identity_repo.update_match_status(
            identity.id, MatchStatus.NEEDS_REVIEW, MatchTier.UNKNOWN
        )

    logger.info(
        "identity_matching_complete",
        playlist_id=str(playlist_id),
        needs_review=len(pending),
    )

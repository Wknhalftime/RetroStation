"""The Resolution Center's decisions on a whole log name: Resolve (matched or rejected) and
Unmatch (phase 1a, C75; S9, S10).

Each runs in the caller's one transaction, which the sync route commits: the status, the
cascade to the name's songs and the name's links (through ``LogNameLinkService``) commit
together or not at all. Nothing here commits or queues work.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from backend.domain.enums import MatchStatus
from backend.domain.matching import LogNameNotFoundError, LogNameNotUnmatchableError
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.artist_resolution import ArtistResolutionRepository
from backend.repositories.log_name_links import LogNameLinkRepository
from backend.services.log_name_link_service import LogNameLinkService


@dataclass(frozen=True)
class ArtistResolutionRepos:
    """The repositories a log-name decision writes through, on one connection."""

    resolution: ArtistResolutionRepository
    links: LogNameLinkRepository
    catalog: ArtistCatalogRepository

    def link_service(self) -> LogNameLinkService:
        return LogNameLinkService(self.links, self.catalog)


def _require(broadcast_artist_id: UUID, repos: ArtistResolutionRepos) -> None:
    if not repos.resolution.exists(broadcast_artist_id):
        raise LogNameNotFoundError(f"Artist {broadcast_artist_id} not found")


def match_by_hand(
    broadcast_artist_id: UUID, target: str, repos: ArtistResolutionRepos
) -> list[UUID]:
    """Link the log name to the artist *target* names (an MBID or an ``artists.id``; an
    unknown MBID gets a stub) and mark it manually matched; its songs in review go back to
    the matcher with their stale matches deleted. Returns the playlists that played the name,
    for the caller to queue song matching once this transaction has committed.

    Raises:
        LogNameNotFoundError: no log name has this id.
    """
    _require(broadcast_artist_id, repos)
    repos.resolution.set_status(broadcast_artist_id, MatchStatus.MANUAL_MATCHED)
    repos.link_service().link_by_hand(broadcast_artist_id, target)
    repos.resolution.reopen_songs_in_review(broadcast_artist_id)
    repos.resolution.delete_pending_song_matches(broadcast_artist_id)
    return repos.resolution.playlists_playing(broadcast_artist_id)


def reject_by_hand(broadcast_artist_id: UUID, repos: ArtistResolutionRepos) -> None:
    """Mark the log name manually rejected, and auto-reject its songs the curator has not
    decided.

    Raises:
        LogNameNotFoundError: no log name has this id.
    """
    _require(broadcast_artist_id, repos)
    repos.resolution.set_status(broadcast_artist_id, MatchStatus.MANUAL_REJECTED)
    repos.resolution.reject_open_songs(broadcast_artist_id)


def unmatch(broadcast_artist_id: UUID, repos: ArtistResolutionRepos) -> None:
    """Put a matched or rejected log name and every one of its songs back in review, delete
    the songs' matches and remove every link of the name, in every state.

    Raises:
        LogNameNotFoundError: no log name has this id.
        LogNameNotUnmatchableError: the name is pending or in review.
    """
    if not repos.resolution.unmatch_if_decided(broadcast_artist_id):
        status = repos.resolution.status_of(broadcast_artist_id)
        if status is None:
            raise LogNameNotFoundError(f"Artist {broadcast_artist_id} not found")
        raise LogNameNotUnmatchableError(status.value)
    repos.resolution.reopen_every_song(broadcast_artist_id)
    repos.resolution.delete_song_matches(broadcast_artist_id)
    repos.link_service().unlink_all(broadcast_artist_id)

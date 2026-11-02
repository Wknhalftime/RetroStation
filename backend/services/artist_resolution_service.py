"""The Resolution Center's decisions on a whole log name: Resolve (matched or rejected) and
Unmatch (phase 1a, C75; S9, S10), and the off-air preview of Reject and Unmatch (phase 1b, S5).

Each runs in the caller's one transaction, which the sync route commits: the status, the
cascade to the name's songs and the name's links (through ``LogNameLinkService``) commit
together or not at all. Nothing here commits or queues work.

Reject and Unmatch cascade safely (phase 1b, M24, AUD-R037): ``plan_cascade`` keeps the
manual lines and the lines matched to a same-name file, and the name's links are remembered as
rejected (C19). Each answers with the counts of what it did, read when it runs (E2).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from backend.domain.artist_cascade import CascadeCounts, CascadePlan, plan_cascade
from backend.domain.enums import CascadeAction, MatchStatus
from backend.domain.matching import LogNameNotFoundError, LogNameNotUnmatchableError
from backend.repositories.artist_cascade import ArtistCascadeRepository
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.artist_resolution import ArtistResolutionRepository
from backend.repositories.log_name_links import LogNameLinkRepository
from backend.services.log_name_link_service import LogNameLinkService


@dataclass(frozen=True)
class ArtistResolutionRepos:
    """The repositories a log-name decision writes through, on one connection, and that
    connection's commit: the route commits before it queues song matching, and a lost
    connection is ``StorageUnavailableError``."""

    resolution: ArtistResolutionRepository
    links: LogNameLinkRepository
    catalog: ArtistCatalogRepository
    cascade: ArtistCascadeRepository
    commit: Callable[[], None]

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


def _locked_plan(
    broadcast_artist_id: UUID, action: CascadeAction, repos: ArtistResolutionRepos
) -> CascadePlan:
    """The plan on the name's lines as they are now, held until the transaction ends, so a
    matcher's guarded write waits for the action instead of being overwritten (AUD-R018).
    Called after the name's own write, in the matcher's order (name, then lines)."""
    return plan_cascade(action, repos.cascade.lines_of(broadcast_artist_id, lock=True))


def cascade_preview(
    broadcast_artist_id: UUID, action: CascadeAction, repos: ArtistResolutionRepos
) -> CascadeCounts:
    """What *action* would take off air and keep on air, read now and written nowhere (S5).

    Raises:
        LogNameNotFoundError: no log name has this id.
    """
    _require(broadcast_artist_id, repos)
    return plan_cascade(action, repos.cascade.lines_of(broadcast_artist_id)).counts()


def reject_by_hand(broadcast_artist_id: UUID, repos: ArtistResolutionRepos) -> CascadeCounts:
    """Mark the log name manually rejected, reject its links (C19) and auto-reject the lines
    the plan changes; manual and same-name lines stay as they are (S2). Returns what it took
    off air and kept (E2).

    Raises:
        LogNameNotFoundError: no log name has this id.
    """
    _require(broadcast_artist_id, repos)
    # The name's row first, then its lines: the artist matcher's order, so the two never
    # wait on each other (a deadlock would fail one of them).
    repos.resolution.set_status(broadcast_artist_id, MatchStatus.MANUAL_REJECTED)
    plan = _locked_plan(broadcast_artist_id, CascadeAction.REJECT, repos)
    repos.cascade.reject_lines(
        [line.identity_id for line in plan.changed if line.status != MatchStatus.AUTO_REJECTED]
    )
    repos.link_service().reject_all(broadcast_artist_id)
    return plan.counts()


def unmatch(broadcast_artist_id: UUID, repos: ArtistResolutionRepos) -> CascadeCounts:
    """Put a matched or rejected log name back in review, reject its links (C19), and put the
    lines the plan changes back in review with their matches deleted; manual and same-name
    lines keep their status and matches (S3). Returns what it took off air and kept (E2).

    Raises:
        LogNameNotFoundError: no log name has this id.
        LogNameNotUnmatchableError: the name is pending or in review.
    """
    if not repos.resolution.unmatch_if_decided(broadcast_artist_id):
        status = repos.resolution.status_of(broadcast_artist_id)
        if status is None:
            raise LogNameNotFoundError(f"Artist {broadcast_artist_id} not found")
        raise LogNameNotUnmatchableError(status.value)
    plan = _locked_plan(broadcast_artist_id, CascadeAction.UNMATCH, repos)
    changed = [line.identity_id for line in plan.changed]
    repos.cascade.reopen_lines(changed)
    repos.cascade.delete_matches_of_lines(changed)
    repos.link_service().reject_all(broadcast_artist_id)
    return plan.counts()

from __future__ import annotations

from uuid import UUID

import structlog

from backend.config import get_settings
from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.db.repositories.mapping_rules import PgMappingRuleRepository
from backend.db.repositories.matches import PgMatchRepository
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.db.sync_conn import connect_sync
from backend.domain.enums import LogCategory, MatchStatus, ReasonCode, TaskType
from backend.services.artist_matching_service import (
    ArtistMatchingRepos,
    ArtistMatchThresholds,
    match_artists_for_playlist,
)
from backend.services.mb_client import MusicBrainzApiClient
from backend.tasks._enqueue_chain import enqueue_or_log
from backend.tasks._error_boundary import task_failure_telemetry
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def artist_matching_task(playlist_id: str) -> None:
    """Run artist matching for all PENDING artists in this playlist.

    Before matching, reset any DEFERRED_RETRY artists (and their child
    identities) whose IDs appear in this playlist's batch back to PENDING —
    gives the local engine a second chance now that the catalog may have
    grown since the prior attempt.

    Wraps the pipeline stage in `task_failure_telemetry` so any exception
    (repo error, MB client failure, UUID parse error) is surfaced as a
    FAILED TaskProgress row + ERROR SystemLog before re-raising to Huey.
    """
    settings = get_settings()
    pid = UUID(playlist_id)

    with task_failure_telemetry(TaskType.MATCHING, LogCategory.MATCHING) as task_id:
        with (
            connect_sync(settings.database_url) as conn,
            MusicBrainzApiClient(
                PgMusicBrainzCacheRepository(conn),
                ttl_days=settings.mb_cache_ttl_days,
            ) as mb_client,
        ):
            # `with MusicBrainzApiClient(...)` is required — the client owns an
            # httpx.Client that only closes in __exit__. Without the context
            # manager a long-lived Huey worker leaks HTTP connections across
            # task runs.
            broadcast_artist_repo = PgBroadcastArtistRepository(conn)
            track_identity_repo = PgBroadcastTrackIdentityRepository(conn)

            # Reset scope is intentionally per-playlist: derived from
            # get_all_for_playlist(pid). See plan §"Reset scope rationale".
            playlist_artists = broadcast_artist_repo.get_all_for_playlist(pid)
            deferred_artist_ids = [
                a.id
                for a in playlist_artists
                if a.match_status == MatchStatus.NEEDS_REVIEW
                and a.reason_code == ReasonCode.DEFERRED_RETRY
            ]
            artists_reset = broadcast_artist_repo.reset_deferred_by_ids(deferred_artist_ids)
            identities_reset = track_identity_repo.reset_deferred_by_artist_ids(deferred_artist_ids)
            logger.info(
                "deferred_reset_summary",
                playlist_id=playlist_id,
                artists_reset=artists_reset,
                identities_reset=identities_reset,
            )

            repos = ArtistMatchingRepos(
                broadcast_artist_repo=broadcast_artist_repo,
                track_identity_repo=track_identity_repo,
                artist_repo=PgArtistRepository(conn),
                match_repo=PgMatchRepository(conn),
                rules_repo=PgMappingRuleRepository(conn),
            )
            thresholds = ArtistMatchThresholds(
                strong_match_threshold=settings.strong_match_threshold,
                mb_score_gap=settings.mb_score_gap,
                mb_auto_link_score=settings.mb_auto_link_score,
                min_presentation_score=settings.min_presentation_score,
                broadcast_name_max_len=settings.broadcast_name_max_len,
            )
            match_artists_for_playlist(
                playlist_id=pid,
                repos=repos,
                mb_client=mb_client,
                thresholds=thresholds,
            )
            conn.commit()

        logger.info(
            "artist_matching_task_complete",
            playlist_id=playlist_id,
            task_id=task_id,
        )

    # Hand off to identity matching, which needs this run's committed artist
    # results. Outside the telemetry boundary and guarded (AUD-R012 (1),
    # AUD-R014): an enqueue failure is logged on this run's own task_id and is
    # not an artist-matching failure.
    from backend.tasks.identity_matching_tasks import identity_matching_task

    with connect_sync(settings.database_url, autocommit=True) as log_conn:
        enqueue_or_log(
            lambda: identity_matching_task(playlist_id),
            task_name="identity_matching_task",
            caller_task_id=task_id,
            log_category=LogCategory.MATCHING,
            sys_log_repo=PgSystemLogRepository(log_conn),
        )

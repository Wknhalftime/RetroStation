"""Give local catalog artists the MusicBrainz ID their own files carry (AUD-R026; spec D15).

The one producer: mb_enrichment_task queues link_local_artists_task() just before its re-check
hand-off. On -w 1 the re-check therefore runs after it, and its wave holds the names this run
linked (matching_recheck_service.recheck_names).

One run:
- lists the due local artists (never looked up, or with a present file indexed since);
- decides and writes each one in its own transaction (artist_linking_service.link_artist);
- stops early after MAX_CONSECUTIVE_FAILURES failed lookups in a row (an outage): the rest stay
  due for the next run. Artists decided without a lookup neither count nor reset the run;
- reports a count per outcome. It hands nothing off.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import httpx
import psycopg
import structlog

from backend.config import get_settings
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.db.sync_conn import connect_sync
from backend.domain.catalog import Artist
from backend.domain.enums import ArtistLinkOutcome, LogCategory, TaskType
from backend.repositories.artist_linking import ArtistLinkingRepository
from backend.services.artist_linking_service import link_artist
from backend.services.mb_client import MusicBrainzApiClient, MusicBrainzClientProtocol
from backend.services.repository_factory import RepositoryFactory
from backend.tasks._task_run import TaskLifecycleMessages, TaskRunConfig, task_run
from backend.tasks.huey_app import huey

logger = structlog.get_logger()

FAILED = "failed"
STOPPED_EARLY = "stopped_early"

# Failed lookups in a row after which the run stops: MusicBrainz, a proxy or the network is
# down, and every further artist would only wait out its retries and timeouts.
MAX_CONSECUTIVE_FAILURES = 10

# Outcomes decided without asking MusicBrainz: they neither count toward nor reset the streak.
# Untagged artists sit between the tagged ones in the due order (dev DB: the longest run of
# artists that ask MusicBrainz is 14), so a reset on them would keep the breaker from tripping.
_NO_LOOKUP: frozenset[str] = frozenset(
    {ArtistLinkOutcome.NO_EVIDENCE.value, ArtistLinkOutcome.SPECIAL_PURPOSE.value}
)

# Per-artist failures that leave the artist due for the next run, as in mb_enrichment_task:
# any HTTP failure (a 429/5xx after the client's retries, a 403 from a proxy, a network drop),
# a dropped database write, a malformed MusicBrainz payload. The tag path never sends a
# malformed MBID, so no 4xx is a property of the artist. Logic bugs reach task_run's boundary.
_RETRIABLE: tuple[type[BaseException], ...] = (httpx.HTTPError, psycopg.Error, ValueError)


@huey.task()  # type: ignore[untyped-decorator]
def link_local_artists_task() -> dict[str, int]:
    """Link the due local artists to the MusicBrainz artist their files are tagged with (D15).

    The work list lives in the artists table (AUD-R015), so the message carries nothing. Each
    artist commits on its own, so a crash keeps every artist already done.
    """
    settings = get_settings()
    tally: Counter[str] = Counter()
    total = 0
    stopped_early = 0
    summary: dict[str, int] = {}

    config = TaskRunConfig(
        task_type=TaskType.ARTIST_LINKING,
        log_category=LogCategory.ENRICHMENT,
        messages=TaskLifecycleMessages(
            started="artist_linking_started",
            completed="artist_linking_completed",
            failed="artist_linking_failed",
        ),
        read_progress=lambda: (sum(tally.values()), total),
    )

    with task_run(settings.database_url, config) as handle:
        with connect_sync(settings.database_url) as conn:
            repo = RepositoryFactory(conn).artist_linking
            due = repo.list_due()
            conn.commit()  # end the listing's transaction: each artist gets its own
            total = len(due)
            handle.report_running(
                progress_data={"processed": 0, "total": total, "current_item": ""}
            )
            handle.log_started(details={"artists": total})
            cache_repo = PgMusicBrainzCacheRepository(conn)
            streak = 0
            with MusicBrainzApiClient(cache_repo, ttl_days=settings.mb_cache_ttl_days) as mb_client:
                for artist in due:
                    outcome = _link_one(artist, repo, mb_client, conn)
                    tally[outcome] += 1
                    if outcome == FAILED:
                        streak += 1
                    elif outcome not in _NO_LOOKUP:  # only an artist that reached MusicBrainz
                        streak = 0
                    handle.report_running(
                        progress_data={
                            "processed": sum(tally.values()),
                            "total": total,
                            "current_item": artist.name,
                            **tally,
                        }
                    )
                    if streak >= MAX_CONSECUTIVE_FAILURES:
                        stopped_early = 1
                        logger.warning("artist_linking_stopped_early", failed_in_a_row=streak)
                        break
        summary = {
            "artists": total,
            **{outcome.value: tally[outcome.value] for outcome in ArtistLinkOutcome},
            FAILED: tally[FAILED],
            STOPPED_EARLY: stopped_early,
        }
        handle.set_completed(
            progress_data={"processed": sum(tally.values()), "total": total, **summary},
            details=summary,
        )

    logger.info("artist_linking_task_complete", **summary)
    return summary


def _link_one(
    artist: Artist,
    repo: ArtistLinkingRepository,
    mb_client: MusicBrainzClientProtocol,
    conn: psycopg.Connection[Any],
) -> str:
    """Link or record one artist in its own transaction; return its outcome, or "failed"."""
    try:
        outcome = link_artist(artist, repo, mb_client)
    except _RETRIABLE as exc:
        conn.rollback()
        logger.warning(
            "artist_linking_failed", artist_id=artist.id, name=artist.name, error=str(exc)
        )
        return FAILED
    conn.commit()
    return outcome.value

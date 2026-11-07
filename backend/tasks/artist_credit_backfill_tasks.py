"""The run-once backfill of artist MBIDs, credits and names (phase 2a S3; C26, C43, C69).

It fills ``artist_mbids`` and ``credit``, then S13's stubs and S10's names of the artists the
catalog holds, runs S14's sweep, and adds the aliases and tag spellings of what the catalog now
holds. A done-marker in ``user_settings`` ends it for good: the scan keeps the columns from then
on. MusicBrainz down leaves those files for the next run, with the sweep and the marker waiting
for them (AUD-R028), and the result says ``done: false``. Nothing queues it: the cutover and the
gate run it by name. The cutover runs it before scans resume after 0048, or a scan's sweep stubs
the MBIDs whose names this would have found: all of them on a full scan, and a watcher batch those
of the files it re-reads.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg
import structlog

from backend.config import get_settings
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.db.sync_conn import connect_sync
from backend.domain.enums import ArtistNameSource, LogCategory, TaskType
from backend.domain.library import ARTIST_CREDITS_BACKFILLED_SETTING
from backend.domain.system import UserSetting
from backend.services.artist_credit_backfill_service import (
    BackfillPorts,
    BackfillTally,
    fill_batch,
)
from backend.services.mb_client import MusicBrainzApiClient
from backend.services.normalization import normalize_artist
from backend.services.repository_factory import RepositoryFactory
from backend.services.shared_name_service import record_different_acts
from backend.tasks._task_run import TaskLifecycleMessages, TaskRunConfig, task_run
from backend.tasks.huey_app import huey

logger = structlog.get_logger()

BATCH_SIZE = 500


def _fill_columns(
    conn: psycopg.Connection[Any],
    ports: BackfillPorts,
    tally: BackfillTally,
    report_progress: Callable[[], None],
) -> None:
    """Every row to fill, a committed batch at a time, so a crash keeps what is done."""
    after: UUID | None = None
    while True:
        batch = ports.files.list_to_fill(after, BATCH_SIZE)
        if not batch:
            return
        fill_batch(batch, ports, tally)
        conn.commit()
        report_progress()
        after = batch[-1].id


def _fill_stubs(repos: RepositoryFactory) -> None:
    """S13's fill: a stub 1a named and left with no normalized name takes it, by S13's rule."""
    for stub in repos.artists.list_named_stubs_without_normalized_name():
        normalized = normalize_artist(stub.name)
        if not normalized:
            continue
        if repos.artists.get_by_normalized_name(normalized) is not None and stub.mbid:
            record_different_acts(repos.library_suggestions, normalized, stub.mbid)
        repos.artists.set_normalized_name(stub.id, normalized)


def _fill_own_names(repos: RepositoryFactory) -> None:
    """S10: each artist's own name, unless a writer since 0048 recorded it. A sweep may have got
    there first (a scan's, or this task's on a run cut short), and its name came from a tag, so it
    mustn't be recorded here as MusicBrainz's."""
    for artist in repos.artists.list_all():
        recorded = repos.artist_names.list_for_artist(artist.id)
        if any(held.name == artist.name for held in recorded):
            continue
        source = ArtistNameSource.MUSICBRAINZ_NAME if artist.mbid else ArtistNameSource.TAG
        repos.artist_names.add(artist.id, artist.name, normalize_artist(artist.name), source)


def _aliases_of(payload: dict[str, object]) -> list[str]:
    aliases = payload.get("aliases")
    if not isinstance(aliases, list):
        return []
    named = (alias for alias in aliases if isinstance(alias, dict))
    return [str(alias["name"]) for alias in named if alias.get("name")]


def _cached_hits(repos: RepositoryFactory) -> list[dict[str, object]]:
    """What the cache has on artists: ``artist-search`` hits, kept whole, and ``artist:`` rows from
    before #231, which still hold aliases. 2a fetches none (C46)."""
    hits = [dict(row) for _, row in repos.mb_cache_listing.rows_with_prefix("artist:")]
    for _, row in repos.mb_cache_listing.rows_with_prefix("artist-search:"):
        found = row.get("artists")
        hits += [dict(h) for h in found if isinstance(h, dict)] if isinstance(found, list) else []
    return hits


def _fill_other_names(repos: RepositoryFactory) -> None:
    """S10: the cached aliases of an artist the catalog holds, and the tag spellings paired with its
    MBID, after the sweep, so they reach the artists it made. An unnamed stub counts: this reads
    them once, and enrichment naming it later adds only MusicBrainz's. A log spelling is never
    read."""
    hits = _cached_hits(repos)
    spellings = repos.artist_credit_files.list_tag_spellings()
    asked = {str(hit.get("id")) for hit in hits} | {mbid for mbid, _ in spellings}
    held = repos.artists.artist_ids_by_mbid(asked)
    names = repos.artist_names
    for hit in hits:
        artist_id = held.get(str(hit.get("id")))
        if artist_id is not None:
            for alias in _aliases_of(hit):
                normalized = normalize_artist(alias)
                names.add(artist_id, alias, normalized, ArtistNameSource.MUSICBRAINZ_ALIAS)
    for mbid, spelling in spellings:
        artist_id = held.get(mbid)
        if artist_id is not None:
            names.add(artist_id, spelling, normalize_artist(spelling), ArtistNameSource.TAG)


def _run_backfill(
    conn: psycopg.Connection[Any],
    tally: BackfillTally,
    ttl_days: int,
    report_progress: Callable[[], None],
) -> dict[str, object]:
    """All of it, on one connection; what it did."""
    # Local import: the sweep's module is imported from huey_app's, like this one.
    from backend.tasks.artist_identity_tasks import sweep_artist_identities

    repos = RepositoryFactory(conn)
    with MusicBrainzApiClient(PgMusicBrainzCacheRepository(conn), ttl_days=ttl_days) as client:
        ports = BackfillPorts(repos.artist_credit_files, repos.mb_cache_listing, client)
        _fill_columns(conn, ports, tally, report_progress)
    logger.info("artist_credits_filled", **asdict(tally))
    _fill_stubs(repos)
    _fill_own_names(repos)
    conn.commit()
    # E3: while a file waits for MusicBrainz, so do the stubs. A scan's sweep doesn't.
    sweep = sweep_artist_identities(conn, repos) if tally.waiting == 0 else None
    _fill_other_names(repos)
    done = sweep is not None
    if done:
        marker = UserSetting(ARTIST_CREDITS_BACKFILLED_SETTING, datetime.now(UTC).isoformat())
        repos.user_settings.upsert(marker)
    conn.commit()
    return {**asdict(tally), "stubbed": sweep.stubs_made if sweep is not None else 0, "done": done}


@huey.task()  # type: ignore[untyped-decorator]
def backfill_artist_credits_task() -> dict[str, object]:
    """Run the backfill, or do nothing once its marker is set; what it did."""
    settings = get_settings()
    with connect_sync(settings.database_url) as conn:
        if RepositoryFactory(conn).user_settings.get(ARTIST_CREDITS_BACKFILLED_SETTING) is not None:
            return {**asdict(BackfillTally()), "stubbed": 0, "done": True}

    tally = BackfillTally()
    config = TaskRunConfig(
        task_type=TaskType.ARTIST_CREDITS_BACKFILL,
        log_category=LogCategory.ENRICHMENT,
        messages=TaskLifecycleMessages(
            started="artist_credits_backfill_started",
            completed="artist_credits_backfill_completed",
            failed="artist_credits_backfill_failed",
        ),
        read_progress=lambda: (tally.files, tally.files),
    )
    with task_run(settings.database_url, config) as handle:

        def report_progress() -> None:
            # A total of 0 pulses the bar: a run can't tell how many rows it will fill, because a
            # lookup that fails leaves its file for the next run.
            handle.report_running(
                progress_data={"processed": tally.files, "total": 0, "current_item": ""}
            )

        report_progress()
        handle.log_started(details={})
        with connect_sync(settings.database_url) as conn:
            result = _run_backfill(conn, tally, settings.mb_cache_ttl_days, report_progress)
        handle.set_completed(
            progress_data={"processed": tally.files, "total": tally.files, **result},
            details=result,
        )
    logger.info("artist_credits_backfill_complete", **result)
    return result

"""The run-once backfill of artist MBIDs, credits and names (phase 2a S3; C26, C43, C69).

It fills ``artist_mbids`` and ``credit``, then S13's stubs and S10's names of the artists the
catalog holds, runs S14's sweep, and adds the aliases and tag spellings of what the catalog now
holds. A done-marker in ``user_settings`` ends it for good: the scan keeps the columns from then
on. MusicBrainz down leaves those files for the next run, with the sweep and the marker waiting
for them (AUD-R028). Nothing queues it: the cutover and the gate run it once, by name, and the
cutover runs it before the first full scan after 0048, or that scan's sweep stubs the MBIDs whose
names this would have found.
"""

from __future__ import annotations

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
from backend.services.mb_client import MusicBrainzApiClient, MusicBrainzClientProtocol
from backend.services.normalization import normalize_artist
from backend.services.repository_factory import RepositoryFactory
from backend.services.shared_name_service import record_different_acts
from backend.tasks._task_run import TaskLifecycleMessages, TaskRunConfig, task_run
from backend.tasks.huey_app import huey

logger = structlog.get_logger()

BATCH_SIZE = 500


def _fill_columns(
    conn: psycopg.Connection[Any],
    repos: RepositoryFactory,
    client: MusicBrainzClientProtocol,
    tally: BackfillTally,
) -> None:
    """Every row to fill, a committed batch at a time, so a crash keeps what is done."""
    ports = BackfillPorts(repos.artist_credit_files, repos.mb_cache_listing, client)
    after: UUID | None = None
    while True:
        batch = repos.artist_credit_files.list_to_fill(after, BATCH_SIZE)
        if not batch:
            return
        fill_batch(batch, ports, tally)
        conn.commit()
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
    """S10: each artist the catalog holds before the sweep, its own name. An artist the sweep makes
    writes its own, from a tag, so it mustn't be recorded here as MusicBrainz's."""
    for artist in repos.artists.list_all():
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
    MBID, after the sweep, so they reach the artists it made. A log spelling is never read."""
    by_mbid = {artist.mbid: artist for artist in repos.artists.list_all() if artist.mbid}
    names = repos.artist_names
    for hit in _cached_hits(repos):
        held = by_mbid.get(str(hit.get("id")))
        if held is not None:
            for alias in _aliases_of(hit):
                normalized = normalize_artist(alias)
                names.add(held.id, alias, normalized, ArtistNameSource.MUSICBRAINZ_ALIAS)
    for mbid, spelling in repos.artist_credit_files.list_tag_spellings():
        held = by_mbid.get(mbid)
        if held is not None:
            names.add(held.id, spelling, normalize_artist(spelling), ArtistNameSource.TAG)


def _run_backfill(
    conn: psycopg.Connection[Any], tally: BackfillTally, ttl_days: int
) -> dict[str, object]:
    """All of it, on one connection; what it did."""
    # Local import: the sweep's module is imported from huey_app's, like this one.
    from backend.tasks.artist_identity_tasks import sweep_artist_identities

    repos = RepositoryFactory(conn)
    with MusicBrainzApiClient(PgMusicBrainzCacheRepository(conn), ttl_days=ttl_days) as client:
        _fill_columns(conn, repos, client, tally)
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
        handle.log_started(details={})
        with connect_sync(settings.database_url) as conn:
            result = _run_backfill(conn, tally, settings.mb_cache_ttl_days)
        handle.set_completed(
            progress_data={"processed": tally.files, "total": tally.files, **result},
            details=result,
        )
    logger.info("artist_credits_backfill_complete", **result)
    return result

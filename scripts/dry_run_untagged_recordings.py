"""What the untagged-file enrichment pass would link, before it runs for real.

Pass 4 of library enrichment (``untagged_recording_service``) searches
MusicBrainz recordings by (artist MBID, title) for files whose tags carry no
MBID but whose work's artist has one, and links a file only when the
confidence rule (``pick_recording``) leaves one recording. This reports, for
every candidate file, what that rule would decide from the search results, so
the first live run is read before it is made.

Run: uv run python scripts/dry_run_untagged_recordings.py                   # cached searches only
     uv run python scripts/dry_run_untagged_recordings.py --max-searches 300 # plus 300 live ones
     uv run python scripts/dry_run_untagged_recordings.py --report out.md   # write the file list
     uv run python scripts/dry_run_untagged_recordings.py --apply           # run the pass, commit

Targets ``DATABASE_URL`` if set, else the app's configured database. The dry
run reads ``mb_cache`` first and calls MusicBrainz only for pairs not cached,
up to --max-searches (default 0: no call); the results it fetches are cached
as the client always caches them, so a later run (or the pass itself) does
not fetch them again. It writes nothing else. --apply runs the pass over
every pair, committing after each, as the enrichment task does; run it while
the worker is idle, on the owner's go-ahead.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import Settings  # noqa: E402
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository  # noqa: E402
from backend.services.library_enrichment_service import EnrichmentRepos  # noqa: E402
from backend.services.mb_client import MusicBrainzApiClient  # noqa: E402
from backend.services.mb_types import MbRecording  # noqa: E402
from backend.services.repository_factory import RepositoryFactory  # noqa: E402
from backend.services.untagged_recording_service import (  # noqa: E402
    PickOutcome,
    UntaggedPair,
    enrich_untagged_pair,
    pick_recording,
    untagged_pairs,
)

_SEARCH_LIMIT = 10


def _search_key(pair: UntaggedPair) -> str:
    """The client's cache key for this pair's search (``MusicBrainzApiClient.search_recording``)."""
    return f"recording-search:{pair.artist_mbid}:{pair.title.lower()}:{_SEARCH_LIMIT}"


@dataclass
class _Tally:
    files: int = 0
    pairs: int = 0
    pairs_cached: int = 0
    pairs_fetched: int = 0
    pairs_unsearched: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    confident_recordings: set[str] = field(default_factory=set)
    lines: list[str] = field(default_factory=list)


def _hits_for(
    pair: UntaggedPair,
    cache: PgMusicBrainzCacheRepository,
    client: MusicBrainzApiClient,
    budget: list[int],
) -> list[MbRecording] | None:
    """Cached hits, else a live search while the budget lasts, else None (unsearched)."""
    cached = cache.get(_search_key(pair))
    if cached is not None:
        return cast(list[MbRecording], cached.response_data.get("recordings", []))
    if budget[0] <= 0:
        return None
    budget[0] -= 1
    return client.search_recording(artist_mbid=pair.artist_mbid, title=pair.title)


def _dry_run(
    pairs: list[UntaggedPair],
    cache: PgMusicBrainzCacheRepository,
    client: MusicBrainzApiClient,
    max_searches: int,
) -> _Tally:
    tally = _Tally(pairs=len(pairs), files=sum(len(p.files) for p in pairs))
    budget = [max_searches]
    for pair in pairs:
        was_cached = cache.get(_search_key(pair)) is not None
        hits = _hits_for(pair, cache, client, budget)
        if hits is None:
            tally.pairs_unsearched += 1
            tally.outcomes["unsearched"] += len(pair.files)
            continue
        if was_cached:
            tally.pairs_cached += 1
        else:
            tally.pairs_fetched += 1
        for library_file in pair.files:
            pick = pick_recording(library_file, pair.artist_mbid, hits)
            tally.outcomes[pick.outcome.value] += 1
            if pick.recording_mbid is not None:
                tally.confident_recordings.add(pick.recording_mbid)
            tally.lines.append(
                f"| {pick.outcome.value} | {library_file.audio.track_title} | "
                f"{library_file.audio.duration_ms} | {pick.recording_mbid or ''} | "
                f"{len(pick.candidate_mbids)} | {len(hits)} | {library_file.file_path} |"
            )
    return tally


def _print_summary(tally: _Tally, cache: PgMusicBrainzCacheRepository, no_artist_mbid: int) -> None:
    lookups_cached = len(
        cache.get_many([f"recording:{mbid}" for mbid in sorted(tally.confident_recordings)])
    )
    print(f"candidate files:                 {tally.files}")
    print(f"untagged files, artist no MBID:  {no_artist_mbid} (out of scope)")
    print(f"distinct (artist, title) pairs:  {tally.pairs}")
    print(f"  with a cached search:          {tally.pairs_cached}")
    print(f"  searched now:                  {tally.pairs_fetched}")
    print(f"  not searched (budget):         {tally.pairs_unsearched}")
    print("file outcomes on the searched pairs:")
    for outcome in (PickOutcome.CONFIDENT, PickOutcome.AMBIGUOUS, PickOutcome.EMPTY):
        print(f"  {outcome.value:<10} {tally.outcomes[outcome.value]}")
    print(f"  unsearched {tally.outcomes['unsearched']}")
    print(
        f"recording lookups the pass would make: {len(tally.confident_recordings)} "
        f"({lookups_cached} already cached)"
    )
    print(
        f"live calls still needed: {tally.pairs_unsearched} searches + "
        f"{len(tally.confident_recordings) - lookups_cached} lookups"
    )


def _write_report(path: Path, tally: _Tally) -> None:
    header = [
        "| outcome | title | duration_ms | picked recording | candidates | hits | file |",
        "|---|---|---|---|---|---|---|",
    ]
    path.write_text("\n".join(header + tally.lines) + "\n", encoding="utf-8")
    print(f"file list written to {path}")


def _apply(
    pairs: list[UntaggedPair],
    repos: RepositoryFactory,
    client: MusicBrainzApiClient,
    conn: psycopg.Connection[Any],
) -> None:
    enrichment_repos = EnrichmentRepos(
        files=repos.library_files,
        enrichment_queries=repos.library_files,
        recordings=repos.recordings,
        works=repos.works,
        song_masters=repos.song_masters,
        matches=repos.matches,
        artists=repos.artists,
    )
    enriched = skipped = 0
    for index, pair in enumerate(pairs, start=1):
        outcome = enrich_untagged_pair(pair, enrichment_repos, client)
        conn.commit()
        enriched += outcome.enriched
        skipped += len(outcome.skipped)
        if index % 100 == 0:
            print(f"{index}/{len(pairs)} pairs, enriched {enriched}, skipped {skipped}")
    print(f"done: {len(pairs)} pairs, enriched {enriched}, skipped {skipped}")


def _count_no_artist_mbid(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute(
        """SELECT count(*) AS n
           FROM library_files lf
           LEFT JOIN works w ON w.id = lf.work_id
           LEFT JOIN artists a ON a.id = w.artist_id
           WHERE lf.recording_mbid IS NULL AND lf.release_mbid IS NULL
             AND lf.enrichment_status = 'pending'
             AND lf.file_status = 'present'
             AND a.mbid IS NULL"""
    ).fetchone()
    return int(row["n"]) if row else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--max-searches", type=int, default=0, help="live searches allowed")
    parser.add_argument("--report", type=Path, help="write the per-file list here (markdown)")
    parser.add_argument("--apply", action="store_true", help="run the pass and commit")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL") or Settings().database_url
    settings = Settings()
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        cache = PgMusicBrainzCacheRepository(conn)
        pairs = untagged_pairs(repos.library_files.list_untagged_with_linked_artist())
        no_artist_mbid = _count_no_artist_mbid(conn)
        with MusicBrainzApiClient(cache, ttl_days=settings.mb_cache_ttl_days) as client:
            if args.apply:
                _apply(pairs, repos, client, conn)
                return 0
            tally = _dry_run(pairs, cache, client, args.max_searches)
            # Only the client's cache writes are pending; keep them for the next run.
            conn.commit()
        _print_summary(tally, cache, no_artist_mbid)
        if args.report is not None:
            _write_report(args.report, tally)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    raise SystemExit(main())

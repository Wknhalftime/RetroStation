"""Mark recordings live that MusicBrainz says are live.

Enrichment used to read a recording's version off its MusicBrainz title
alone. MusicBrainz titles a live recording with the plain song name and puts
"live" in its disambiguation, so those recordings were stored as "original"
and the work page grouped live copies with the studio ones. This re-reads
every MusicBrainz-backed recording that is not yet live through the batched
recording search (100 per request, cached) and marks it live when its
disambiguation says so.

Run: uv run python scripts/backfill_live_versions.py            # dry run: report only
     uv run python scripts/backfill_live_versions.py --apply    # write the changes

Targets ``DATABASE_URL`` if set, else the app's configured database. Needs
migration 0038 (restart the API once): a search hit cached before it has no
disambiguation, so it would never read as live. The dry run still fills the
MusicBrainz cache, so --apply afterwards makes no new requests.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import Settings  # noqa: E402
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository  # noqa: E402
from backend.domain.enums import VersionType  # noqa: E402
from backend.services.library_enrichment_service import recording_version_type  # noqa: E402
from backend.services.mb_client import MusicBrainzApiClient  # noqa: E402

_REQUIRED_MIGRATION = "0038_purge_slim_recording_search_cache"
_CHUNK = 500  # recordings per cache commit; the client sends 100 per request


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    dsn = os.environ.get("DATABASE_URL") or Settings().database_url

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s", (_REQUIRED_MIGRATION,)
        ).fetchone()
        if applied is None:
            sys.exit(f"migration {_REQUIRED_MIGRATION} is not applied; restart the API first")

        rows = conn.execute(
            """
            SELECT r.id, r.title, MIN(a.name) AS artist
              FROM recordings r
              JOIN library_files lf ON lf.recording_mbid = r.id
              LEFT JOIN artists a ON a.mbid = lf.artist_mbid
             WHERE r.version_type <> %s
             GROUP BY r.id, r.title
             ORDER BY MIN(a.name), r.title
            """,
            (VersionType.LIVE.value,),
        ).fetchall()
        print(f"MusicBrainz recordings not marked live: {len(rows)}")

        live: list[dict[str, str]] = []
        with MusicBrainzApiClient(PgMusicBrainzCacheRepository(conn)) as client:
            for start in range(0, len(rows), _CHUNK):
                chunk = rows[start : start + _CHUNK]
                found = client.search_recordings_by_mbids([r["id"] for r in chunk])
                conn.commit()  # keep the cache entries even if a later chunk fails
                live.extend(
                    r
                    for r in chunk
                    if r["id"] in found
                    and recording_version_type(found[r["id"]]) == VersionType.LIVE
                )
                print(f"  searched {min(start + _CHUNK, len(rows))}/{len(rows)}", flush=True)

        print(f"recordings to mark live: {len(live)}")
        for r in live:
            print(f"  {r['artist']}: {r['title']!r} ({r['id']})")

        if not args.apply:
            print("\ndry run; pass --apply to write")
            return
        conn.execute(
            "UPDATE recordings SET version_type = %s WHERE id = ANY(%s)",
            (VersionType.LIVE.value, [r["id"] for r in live]),
        )
        conn.commit()
        print(f"\nmarked {len(live)} recordings live")


if __name__ == "__main__":
    main()

"""Name the stub artists on a gate copy (artist model phase 1a, PR 2; SPEC S4, S16, E1-E3).

    python scripts/name_stubs.py --dsn <bench db>

A stub is an artist whose name is still its MBID: migration 0044 made one for each MBID the
artist rows of ``matches`` named and no catalog artist held (S3). Each is looked up on
MusicBrainz, through the production client (its cache, 1.1 s apart) and named by enrichment's
``_enhance_artist``, one transaction per stub. Stubs are selected by name, not by
``needs_enhancement``: enrichment before S4 could mark a stub enhanced without naming it. One
whose ``enhancement_error`` is set (a 404 or a malformed MBID, E3) is not looked up again.

When MusicBrainz is down or slow (E2), that stub is rolled back and stays a stub, the run goes
on, and it exits 1 naming the ones left; the next run names them. It runs in process with the
hand-offs stubbed (as ``gate.py run-task``), so nothing is queued: none of the linking and
re-check a queued enrichment run hands off. Later stubs (S6, S9) are named by enrichment as it
runs. Like the gate tools it refuses a database without "bench" in its name (0b S10).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import structlog
from psycopg.rows import DictRow, dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_common  # noqa: E402

if __name__ == "__main__":
    gate_common.guard_from_argv(sys.argv[1:])  # S10: before the backend imports below

from gate import stubbed_hand_offs  # noqa: E402

from backend.config import get_settings  # noqa: E402
from backend.db.repositories.artists import artist_from_row  # noqa: E402
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository  # noqa: E402
from backend.services.mb_client import (  # noqa: E402
    MusicBrainzApiClient,
    MusicBrainzClientProtocol,
)
from backend.services.repository_factory import RepositoryFactory  # noqa: E402
from backend.tasks.mb_enrichment_tasks import (  # noqa: E402
    _PER_ITEM_RETRIABLE_ERRORS,
    ArtistEnhanceOutcome,
    _enhance_artist,
)

logger = structlog.get_logger()

_STUBS_SQL = """
SELECT * FROM artists
 WHERE name = mbid
   AND enhancement_error IS NULL
 ORDER BY id
"""

_NAMED_SQL = """
SELECT id, mbid, name, normalized_name FROM artists WHERE id = ANY(%s) ORDER BY name, id
"""


@dataclass(frozen=True)
class StubNaming:
    """What one run did, by artist id."""

    named: tuple[str, ...]
    failed: tuple[str, ...]
    """Recorded in ``enhancement_error`` (a 404 or a malformed MBID); never looked up again."""
    left: tuple[str, ...]
    """MusicBrainz did not answer: still stubs, for the next run."""


def name_stubs(conn: psycopg.Connection[DictRow], client: MusicBrainzClientProtocol) -> StubNaming:
    """Name every unnamed stub on *conn* from *client*, committing each one."""
    stubs = [artist_from_row(row) for row in conn.execute(_STUBS_SQL).fetchall()]
    conn.commit()
    repos = RepositoryFactory(conn)
    named: list[str] = []
    failed: list[str] = []
    left: list[str] = []
    for stub in stubs:
        try:
            outcome = _enhance_artist(stub, client, conn, repos)
            conn.commit()
        except _PER_ITEM_RETRIABLE_ERRORS as exc:
            conn.rollback()
            left.append(stub.id)
            logger.warning(
                "stub_naming_deferred", artist_id=stub.id, mbid=stub.mbid, error=str(exc)
            )
            continue
        (named if outcome is ArtistEnhanceOutcome.ENHANCED else failed).append(stub.id)
    return StubNaming(named=tuple(named), failed=tuple(failed), left=tuple(left))


def render(conn: psycopg.Connection[DictRow], run: StubNaming) -> str:
    rows: list[dict[str, Any]] = conn.execute(_NAMED_SQL, (list(run.named),)).fetchall()
    unset = sum(1 for row in rows if row["normalized_name"] is None)
    lines = [
        f"named {len(run.named)} (no normalized name, held by another artist or empty: {unset}), "
        f"recorded {len(run.failed)}, left for the next run {len(run.left)}",
        *(f"  named {row['id']} {row['mbid']} {row['name']}" for row in rows),
        *(f"  recorded {artist_id}" for artist_id in run.failed),
        *(f"  left {artist_id}" for artist_id in run.left),
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    # The gate reads this through a pipe, cp1252 on Windows: a MusicBrainz name is any Unicode.
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dsn", required=True, help="the gate copy (a bench database)")
    args = parser.parse_args(argv)
    gate_common.point_at(args.dsn)
    settings = get_settings()
    started = time.perf_counter()
    with (
        psycopg.connect(args.dsn, row_factory=dict_row) as conn,
        gate_common.quiet_logging(),
        stubbed_hand_offs(),
        MusicBrainzApiClient(
            PgMusicBrainzCacheRepository(conn), ttl_days=settings.mb_cache_ttl_days
        ) as client,
    ):
        run = name_stubs(conn, client)
        report = render(conn, run)
        conn.rollback()
    print(f"{report}\n{time.perf_counter() - started:.1f} s")
    return 1 if run.left else 0


if __name__ == "__main__":
    sys.exit(main())

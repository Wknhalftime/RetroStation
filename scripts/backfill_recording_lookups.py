"""Apply what the cached recording lookups already say: performed work, live flag, length.

The recordings phase of the MusicBrainz enrichment pass used to read only
the length off a recording lookup. The payloads it cached carry more: a
``performance`` relation naming the work the recording performs, and a
``live`` attribute on it. The batched recording search that links most files
carries neither, so those recordings sit on no MusicBrainz work and keep the
version their title gave them (a 2026-11-02 profile of the dev DB: 7,026 of
11,115 cached lookups name a work the recording is not on; 43 say live where
"original" is stored). This runs ``apply_recording_lookup``, the same step the
pass now runs after a lookup, over every cached payload, so nothing is fetched.

Run: uv run python scripts/backfill_recording_lookups.py            # dry run: report only
     uv run python scripts/backfill_recording_lookups.py --apply    # write the changes

Targets ``DATABASE_URL`` if set, else the app's configured database. The dry
run is read-only: it plans every write from the stored rows and reports the
counts. --apply commits every 200 recordings, so a failure keeps what was
done; rerunning is safe, a recording already on its work is left alone. Run
it while the worker is idle: each work move settles song masters and empties
local works, the same writes the enrichment pass makes.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import Settings  # noqa: E402
from backend.domain.catalog import Recording  # noqa: E402
from backend.domain.enums import VersionType  # noqa: E402
from backend.services.library_enrichment_service import (  # noqa: E402
    EnrichmentRepos,
    apply_recording_lookup,
    performed_work_of,
    recording_version_type,
)
from backend.services.repository_factory import RepositoryFactory  # noqa: E402

_COMMIT_EVERY = 200


@dataclass(frozen=True)
class _Planned:
    """What applying one cached payload would do, read off the stored rows."""

    work_mbid: str | None
    work_is_new: bool
    already_on_work: bool
    recording_linked: bool
    files_moved: int
    marked_live: bool
    duration_set: bool


@dataclass
class _Totals:
    recordings: int = 0
    with_work: int = 0
    works_new: int = 0
    already_on_work: int = 0
    recordings_linked: int = 0
    version_slot_taken: int = 0
    files_moved: int = 0
    marked_live: int = 0
    duration_set: int = 0

    def add(self, planned: _Planned) -> None:
        self.recordings += 1
        if planned.work_mbid is not None:
            self.with_work += 1
            self.works_new += int(planned.work_is_new)
            self.already_on_work += int(planned.already_on_work)
            self.recordings_linked += int(planned.recording_linked)
            self.version_slot_taken += int(
                not planned.already_on_work and not planned.recording_linked
            )
        self.files_moved += planned.files_moved
        self.marked_live += int(planned.marked_live)
        self.duration_set += int(planned.duration_set)

    def report(self, verb: str) -> str:
        return "\n".join(
            [
                f"cached recording lookups with a stored recording: {self.recordings}",
                f"  payload names a performed work: {self.with_work}",
                f"    works {verb} created: {self.works_new}",
                f"    recordings already on that work: {self.already_on_work}",
                f"    recordings {verb} put on their work: {self.recordings_linked}",
                f"    recordings whose version slot is taken (files move only): "
                f"{self.version_slot_taken}",
                f"  files {verb} moved onto the work: {self.files_moved}",
                f"  recordings {verb} marked live: {self.marked_live}",
                f"  durations {verb} filled: {self.duration_set}",
            ]
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    return parser


def _cached_rows(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    """Every cached recording lookup with its stored recording, oldest cache entry first."""
    return conn.execute(
        """SELECT r.id, r.title, r.work_id, r.duration_ms, r.version_type, c.response_data
             FROM mb_cache c
             JOIN recordings r ON r.id = c.entity_mbid
            WHERE c.cache_key = 'recording:' || r.id
              AND c.response_data IS NOT NULL
            ORDER BY c.cached_at, r.id"""
    ).fetchall()


def _recording(row: dict[str, Any]) -> Recording:
    return Recording(
        id=row["id"],
        title=row["title"],
        work_id=row["work_id"],
        duration_ms=row["duration_ms"],
        version_type=VersionType(row["version_type"]),
    )


class _Planner:
    """Plans the writes from the stored rows without making them.

    Mirrors ``apply_recording_lookup`` and the version-slot rule of
    ``uq_recordings_work_version``: a slot a planned link takes is taken for
    the recordings planned after it, as it would be once written.
    """

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        works = conn.execute("SELECT id, mbid, origin FROM works").fetchall()
        self._work_by_mbid: dict[str, str] = {w["mbid"]: w["id"] for w in works if w["mbid"]}
        self._slots: set[tuple[str, str]] = {
            (r["work_id"], r["version_type"])
            for r in conn.execute(
                "SELECT work_id, version_type FROM recordings WHERE work_id IS NOT NULL"
            ).fetchall()
        }
        self._files: dict[str, Counter[str | None]] = {}
        for f in conn.execute(
            "SELECT recording_id, work_id FROM library_files WHERE recording_id IS NOT NULL"
        ).fetchall():
            self._files.setdefault(f["recording_id"], Counter())[f["work_id"]] += 1

    def plan(self, recording: Recording, payload: dict[str, Any]) -> _Planned:
        duration_set = recording.duration_ms is None and payload.get("length") is not None
        version = recording.version_type
        marked_live = False
        if version != VersionType.LIVE and recording_version_type(payload) == VersionType.LIVE:
            marked_live = (recording.work_id, VersionType.LIVE.value) not in self._slots
            if marked_live:
                version = VersionType.LIVE
        work = performed_work_of(payload)
        if work is None:
            return _Planned(None, False, False, False, 0, marked_live, duration_set)
        work_mbid, _ = work
        work_id = self._work_by_mbid.get(work_mbid)
        work_is_new = work_id is None
        if work_id is None:
            work_id = work_mbid  # a new work's id is unknown until written; any key will do
            self._work_by_mbid[work_mbid] = work_id
        already_on_work = recording.work_id == work_id
        recording_linked = False
        if not already_on_work:
            recording_linked = (work_id, version.value) not in self._slots
            if recording_linked:
                self._slots.add((work_id, version.value))
        files = self._files.get(recording.id, Counter())
        files_moved = sum(n for w, n in files.items() if w != work_id)
        return _Planned(
            work_mbid,
            work_is_new,
            already_on_work,
            recording_linked,
            files_moved,
            marked_live,
            duration_set,
        )


def _dry_run(conn: psycopg.Connection[Any], rows: list[dict[str, Any]]) -> None:
    planner = _Planner(conn)
    totals = _Totals()
    for row in rows:
        totals.add(planner.plan(_recording(row), row["response_data"]))
    print(totals.report("would be"))
    print("\ndry run; pass --apply to write")


def _apply(conn: psycopg.Connection[Any], rows: list[dict[str, Any]]) -> None:
    repos = RepositoryFactory(conn)
    enrichment_repos = EnrichmentRepos(
        files=repos.library_files,
        enrichment_queries=repos.library_files,
        recordings=repos.recordings,
        works=repos.works,
        song_masters=repos.song_masters,
        matches=repos.matches,
        artists=repos.artists,
    )
    known_works = {
        w["mbid"] for w in conn.execute("SELECT mbid FROM works WHERE mbid IS NOT NULL").fetchall()
    }
    totals = _Totals()
    for n, row in enumerate(rows, start=1):
        payload = row["response_data"]
        applied = apply_recording_lookup(_recording(row), payload, enrichment_repos)
        work = performed_work_of(payload)
        work_mbid = work[0] if work is not None else None
        totals.add(
            _Planned(
                work_mbid=work_mbid,
                work_is_new=work_mbid is not None and work_mbid not in known_works,
                already_on_work=applied.work_id is not None and row["work_id"] == applied.work_id,
                recording_linked=applied.recording_linked,
                files_moved=applied.files_moved,
                marked_live=applied.marked_live,
                duration_set=applied.duration_set,
            )
        )
        if work_mbid is not None:
            known_works.add(work_mbid)
        if n % _COMMIT_EVERY == 0:
            conn.commit()
            print(f"  applied {n}/{len(rows)}", flush=True)
    conn.commit()
    print(totals.report("were"))


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings()
    dsn = os.environ.get("DATABASE_URL") or settings.database_url

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        rows = _cached_rows(conn)
        if args.apply:
            _apply(conn, rows)
        else:
            _dry_run(conn, rows)


if __name__ == "__main__":
    main()

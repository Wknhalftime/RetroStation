"""Air-play snapshot of a gate copy, and its compare (phase 0b SPEC 2026-10-28, S1, S2, E1, E5).

    python scripts/airplay_snapshot.py --dsn <bench db> --out <file>
    python scripts/airplay_snapshot.py --compare <old> <new> [--report <file>] [--allow <file>]
        [--work-map <file>] [--mode <section>=<mode> ...]

The file holds a header (outside the compared payload), one dict per section keyed by the
strings ``--mode`` and allowlists use, and a timing record:

- ``air-play``: (station, line) -> the file that plays and its ``file_status``, read from
  ``play_file_resolution`` (0030) for one play of the pair, per play as its COMMENT requires;
- ``work-source``: the same keys -> the work and the source (override, master or none);
- ``matches``: every line -> its status and the file of its best match row;
- ``file-work`` and ``tags``: every present file -> its work; its artist name, normalized
  name and artist MBID;
- ``masters``: work -> master file; ``overrides``: ``<work>/<column>=<value>`` -> file;
- ``missing-matches``: station -> how many lines the station's missing-matches report lists;
- ``names``: every log name -> status, reason and active links (``gate_common.active_links``);
- ``artists``: every artist but an unnamed stub (name = MBID) -> name, MBID and origin.

``timing``: the median milliseconds of the three fenced readers (the M3U repository, the
playable schedule and the cue work list) over a fixed sample of up to 200 pairs.

It reads one REPEATABLE READ, READ ONLY transaction, rolled back, and refuses a DSN without
"bench" (S10). The compare is ``gate_common.run_compare``.
"""

from __future__ import annotations

import argparse
import hashlib
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_common  # noqa: E402

TOOL = "airplay_snapshot"
FORMAT_VERSION = 1
COMPARE = gate_common.CompareSpec(
    tool=TOOL,
    work_keyed=frozenset({"masters", "overrides"}),
    work_valued={"work-source": "work", "file-work": None},
)
TIMING_PAIRS = 200

# One play per (station, line): the view resolves a play by its line and its station's
# format, so every play of a pair resolves alike. OFFSET 0 fences the LATERAL, as the view's
# COMMENT and its readers require.
_PAIRS_SQL = """
WITH pairs AS (
    SELECT DISTINCT ON (pl.station_id, pe.identity_id)
           pl.station_id, pe.identity_id, pe.id AS play_event_id,
           (pe.played_at AT TIME ZONE 'UTC')::date AS play_date
    FROM play_events pe
    JOIN playlists pl ON pl.id = pe.playlist_id
    ORDER BY pl.station_id, pe.identity_id, pe.id
)
SELECT p.station_id, p.identity_id, p.play_event_id, p.play_date,
       res.file_id, res.file_status, res.work_id, res.source
FROM pairs p
LEFT JOIN LATERAL (
    SELECT r.file_id, r.file_status, r.work_id, r.source
    FROM play_file_resolution r
    WHERE r.play_event_id = p.play_event_id
    OFFSET 0
) res ON true
"""

# A line's best match row, ordered as the view orders it.
_MATCHES_SQL = """
SELECT ti.id, ti.match_status, best.library_file_id
FROM track_identities ti
LEFT JOIN LATERAL (
    SELECT m.library_file_id FROM matches m
    WHERE m.identity_id = ti.id AND m.library_file_id IS NOT NULL
    ORDER BY m.confidence_score DESC, m.created_at, m.id
    LIMIT 1
) best ON true
"""

_PRESENT_FILES_SQL = """
SELECT id, work_id, artist_name, normalized_artist_name, artist_mbid
FROM library_files WHERE file_status = 'present'
"""

# The missing-matches report's count (routers/stations.py, get_missing_matches_report): a line
# a station played whose own status or whose name's status is unresolved. Every station, so a
# station with none reads 0.
_UNRESOLVED = ["pending", "needs_review", "auto_rejected", "manual_rejected"]
_MISSING_SQL = """
SELECT s.id, count(m.identity_id) AS missing
FROM stations s
LEFT JOIN (
    SELECT sip.station_id, sip.identity_id
    FROM station_identity_plays sip
    JOIN track_identities ti ON ti.id = sip.identity_id
    JOIN broadcast_artists ba ON ba.id = ti.broadcast_artist_id
    WHERE ti.match_status = ANY(%(unresolved)s) OR ba.match_status = ANY(%(unresolved)s)
) m ON m.station_id = s.id
GROUP BY s.id
"""

# An override's key: its work, then every other column that tells two overrides of one work
# apart (format_name today; a version text later), when set.
_OVERRIDE_VALUE_COLUMNS = {
    "id",
    "work_id",
    "preferred_file_id",
    "notes",
    "created_at",
    "updated_at",
}


@dataclass(frozen=True)
class Pair:
    key: str
    station_id: UUID | None
    play_event_id: UUID
    play_date: date


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _air_play(conn: Any) -> tuple[dict[str, Any], dict[str, Any], list[Pair]]:
    air: dict[str, Any] = {}
    work_source: dict[str, Any] = {}
    pairs: list[Pair] = []
    for row in conn.execute(_PAIRS_SQL).fetchall():
        key = f"{row['station_id']}/{row['identity_id']}"
        air[key] = {"file": _text(row["file_id"]), "file_status": row["file_status"]}
        work_source[key] = {"work": row["work_id"], "source": row["source"]}
        pairs.append(Pair(key, row["station_id"], row["play_event_id"], row["play_date"]))
    return air, work_source, pairs


def _files(conn: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    file_work: dict[str, Any] = {}
    tags: dict[str, Any] = {}
    for row in conn.execute(_PRESENT_FILES_SQL).fetchall():
        file_work[str(row["id"])] = row["work_id"]
        tags[str(row["id"])] = {
            "artist_name": row["artist_name"],
            "normalized_artist_name": row["normalized_artist_name"],
            "artist_mbid": row["artist_mbid"],
        }
    return file_work, tags


def _overrides(conn: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in conn.execute("SELECT * FROM format_overrides").fetchall():
        parts = [str(row["work_id"])]
        for column in sorted(set(row) - _OVERRIDE_VALUE_COLUMNS):
            if row[column] is not None:
                parts.append(f"{column}={row[column]}")
        out["/".join(parts)] = _text(row["preferred_file_id"])
    return out


def _names(conn: Any) -> dict[str, Any]:
    links = gate_common.active_links(conn)
    rows = conn.execute("SELECT id, match_status, reason_code FROM broadcast_artists").fetchall()
    return {
        str(row["id"]): {
            "status": row["match_status"],
            "reason": row["reason_code"],
            "links": links.get(str(row["id"]), {}),
        }
        for row in rows
    }


def _artists(conn: Any) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT id, name, mbid, origin FROM artists WHERE name IS DISTINCT FROM mbid"
    ).fetchall()
    return {
        str(row["id"]): {"name": row["name"], "mbid": row["mbid"], "origin": row["origin"]}
        for row in rows
    }


def _sections(conn: Any) -> tuple[dict[str, Any], list[Pair]]:
    air, work_source, pairs = _air_play(conn)
    file_work, tags = _files(conn)
    matches = {
        str(row["id"]): {"status": row["match_status"], "file": _text(row["library_file_id"])}
        for row in conn.execute(_MATCHES_SQL).fetchall()
    }
    masters = {
        str(row["work_id"]): _text(row["preferred_file_id"])
        for row in conn.execute("SELECT work_id, preferred_file_id FROM song_masters").fetchall()
    }
    missing = {
        str(row["id"]): int(row["missing"])
        for row in conn.execute(_MISSING_SQL, {"unresolved": _UNRESOLVED}).fetchall()
    }
    sections = {
        "air-play": air,
        "work-source": work_source,
        "matches": matches,
        "file-work": file_work,
        "masters": masters,
        "overrides": _overrides(conn),
        "missing-matches": missing,
        "tags": tags,
        "names": _names(conn),
        "artists": _artists(conn),
    }
    return sections, pairs


def timing_sample(pairs: Sequence[Pair]) -> list[Pair]:
    """Up to TIMING_PAIRS pairs, picked by a hash of their key: the same data, the same pairs."""
    ranked = sorted(pairs, key=lambda p: (hashlib.md5(p.key.encode()).hexdigest(), p.key))
    return sorted(ranked[:TIMING_PAIRS], key=lambda p: p.key)


def _readers(conn: Any) -> dict[str, Callable[[Pair], object]]:
    """The three fenced readers of play_file_resolution, each asked about one pair's play."""
    from backend.db.repositories.play_file_resolution import PgPlayFileResolutionRepository
    from backend.db.repositories.playable_schedule import PgPlayableScheduleRepository
    from backend.db.repositories.stream_cue_work import PgCueWorkRepository

    m3u = PgPlayFileResolutionRepository(conn)
    schedule = PgPlayableScheduleRepository(conn)
    cues = PgCueWorkRepository(conn)
    return {
        "m3u": lambda p: m3u.get_for_plays([p.play_event_id]),
        "playable_schedule": lambda p: schedule.get_day(p.station_id, p.play_date),  # type: ignore[arg-type]
        "stream_cue_work": lambda p: cues.scheduled([p.play_date]),
    }


def _timing(conn: Any, pairs: Sequence[Pair]) -> dict[str, Any]:
    sample = timing_sample(pairs)
    readers: dict[str, Any] = {}
    for name, read in sorted(_readers(conn).items()):
        times = []
        for pair in sample:
            started = time.perf_counter()
            read(pair)
            times.append((time.perf_counter() - started) * 1000)
        median = round(statistics.median(times), 3) if times else None
        readers[name] = {"median_ms": median}
    return {"pairs": len(sample), "sample": [p.key for p in sample], "readers": readers}


def record(dsn: str) -> dict[str, Any]:
    """The snapshot of *dsn*: header, sections and timing, from one read-only snapshot."""
    with gate_common.snapshot_read(dsn) as conn, gate_common.quiet_logging():
        head = gate_common.header(TOOL, FORMAT_VERSION, conn)
        sections, pairs = _sections(conn)
        timing = _timing(conn, pairs)
    return {"header": head, "sections": sections, "timing": timing}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dsn", help="the gate copy (a bench database)")
    parser.add_argument("--out", type=Path, help="the snapshot file to write")
    gate_common.add_compare_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.compare:
        return gate_common.run_compare(args, COMPARE)
    if not args.dsn or args.out is None:
        parser.error("--dsn and --out are required (or --compare)")
    gate_common.point_at(args.dsn)
    started = time.perf_counter()
    doc = record(args.dsn)
    gate_common.write_doc(args.out, doc)
    counts = ", ".join(f"{name} {len(rows)}" for name, rows in sorted(doc["sections"].items()))
    print(f"{TOOL}: {counts}; {time.perf_counter() - started:.1f} s -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

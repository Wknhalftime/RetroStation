"""What the file-side version reader fix would regroup, on a gate copy (phase 2b S13, C63, D4).

    python scripts/regroup_dry_run.py --dsn <bench copy> --before <checkout> --out <file>

The reader (``extract_version_info``) gives grouping a file's base title and its recording's
version, and enrichment a MusicBrainz recording's version. A fix to it changes how new and
re-scanned files group; files already grouped keep their work and recording. This reads the
copy once and writes one JSON document, comparing the reader of the ``--before`` checkout (its
``backend/services/normalization.py``, loaded on its own) with this tree's:

- ``files``: every present file, by id: path, title and artist tags, and each reader's
  ``base_title`` and ``version_type``.
- ``differs``: the files whose work or recording grouping would give them differs between the
  two readings. For each side: ``work_id`` (null when grouping would create a work),
  ``work_title`` (that work's, or the one it would create) and ``version_type`` (grouping keys
  a recording by work and version). A file grouping places by a shortcut (its audio's twin, its
  recording MBID on a work) reads no title and is never listed.
- ``recordings``: the MusicBrainz recordings (a present file's recording MBID tag names them)
  whose stored version is the before reading of their title and whose after reading differs,
  so the next enrichment upsert writes the new version. ``slot_taken``: another recording on
  the work already holds it, or another listed one is headed there
  (``uq_recordings_work_version``), so that upsert would fail (phase 2b PR 1, question A).
- ``summary``: the counts, printed with one line per listed file and recording.

Grouping's placement is predicted with its own read-only functions (``grouping_shortcut``,
``planned_work``), against the copy as it stands, so ``differs`` says what a new copy or a
re-scan without a work would get, not how each file's current work came about. One REPEATABLE
READ, READ ONLY transaction, rolled back; a database without "bench" in its name is refused
(AUD-R044, the gate's S10).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_common  # noqa: E402

if __name__ == "__main__":
    gate_common.guard_from_argv(sys.argv[1:])  # S10: before the backend imports below

import psycopg  # noqa: E402

from backend.domain.enums import VersionType  # noqa: E402
from backend.domain.library import LibraryFile  # noqa: E402
from backend.services.grouping_service import grouping_shortcut, planned_work  # noqa: E402
from backend.services.normalization import extract_version_info, normalize_title  # noqa: E402
from backend.services.repository_factory import RepositoryFactory  # noqa: E402

TOOL = "regroup_dry_run"
FORMAT_VERSION = 1
_CHUNK = 1_000

type Reader = Callable[[str], tuple[str, VersionType]]


def load_reader(checkout: Path) -> Reader:
    """``extract_version_info`` of *checkout*'s reader, loaded as a module of its own name, so
    this tree's ``backend.services.normalization`` is untouched."""
    path = checkout / "backend" / "services" / "normalization.py"
    if not path.is_file():
        raise SystemExit(f"--before {checkout}: no backend/services/normalization.py there")
    spec = importlib.util.spec_from_file_location("regroup_before_normalization", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"--before {checkout}: cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reader = getattr(module, "extract_version_info", None)
    if not callable(reader):
        raise SystemExit(f"--before {checkout}: normalization.py has no extract_version_info")
    return cast(Reader, reader)


@dataclass(frozen=True)
class _Placement:
    work_id: str | None
    work_title: str
    version_type: VersionType

    def same_as(self, other: _Placement) -> bool:
        if self.version_type != other.version_type or self.work_id != other.work_id:
            return False
        return self.work_id is not None or (
            normalize_title(self.work_title) == normalize_title(other.work_title)
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "work_title": self.work_title,
            "version_type": self.version_type.value,
        }


type _Reading = tuple[str, VersionType]


def _reading_json(reading: _Reading) -> dict[str, str]:
    return {"base_title": reading[0], "version_type": reading[1].value}


def _present_files(conn: psycopg.Connection[Any], repos: RepositoryFactory) -> list[LibraryFile]:
    """Every present file, through the library-files repository (its row mapping)."""
    ids = [
        UUID(str(row["id"]))
        for row in conn.execute(
            "SELECT id FROM library_files WHERE file_status = 'present' ORDER BY file_path, id"
        ).fetchall()
    ]
    files: list[LibraryFile] = []
    for start in range(0, len(ids), _CHUNK):
        files.extend(repos.library_files.get_by_ids(ids[start : start + _CHUNK]))
    return files


def _place(file: LibraryFile, reading: _Reading, repos: RepositoryFactory) -> _Placement:
    """Where grouping's title path puts *file* when its title reads *reading*."""
    base, version = reading
    work_id = planned_work(base, file.audio.artist_name or "", repos.works)
    work = repos.works.get_by_id(work_id) if work_id is not None else None
    return _Placement(work_id, work.title if work is not None else base, version)


def _would_differ(
    file: LibraryFile, before: _Reading, after: _Reading, repos: RepositoryFactory
) -> tuple[_Placement, _Placement] | None:
    """Each reading's placement of *file*, when grouping would place it differently by them.

    None when the readings agree, when grouping leaves the file ungrouped (no artist or title
    tag, as ``assign_work``), or when a shortcut places it before any title is read.
    """
    if before == after:
        return None
    if not (file.audio.artist_name or "").strip() or not (file.audio.track_title or "").strip():
        return None
    shortcut = grouping_shortcut(
        file, library_file_repo=repos.library_files, recording_repo=repos.recordings
    )
    if shortcut is not None:
        return None
    placed_before, placed_after = _place(file, before, repos), _place(file, after, repos)
    return None if placed_before.same_as(placed_after) else (placed_before, placed_after)


def _file_sections(
    conn: psycopg.Connection[Any], repos: RepositoryFactory, before: Reader
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """``files`` and ``differs``: every present file's two readings, and the files whose
    placement they change."""
    files: dict[str, dict[str, Any]] = {}
    differs: dict[str, dict[str, Any]] = {}
    for lf in _present_files(conn, repos):
        title = lf.audio.track_title or ""
        was, now = before(title), extract_version_info(title)
        files[str(lf.id)] = {
            "path": lf.file_path,
            "title": lf.audio.track_title,
            "artist": lf.audio.artist_name,
            "before": _reading_json(was),
            "after": _reading_json(now),
        }
        placed = _would_differ(lf, was, now, repos)
        if placed is not None:
            differs[str(lf.id)] = {"before": placed[0].to_json(), "after": placed[1].to_json()}
    return files, differs


_TAGGED_RECORDINGS_SQL = """
SELECT r.id, r.title, r.work_id, r.version_type FROM recordings r
WHERE r.work_id IS NOT NULL
  AND EXISTS (SELECT 1 FROM library_files f
              WHERE f.recording_mbid = r.id AND f.file_status = 'present')
ORDER BY r.id
"""

_SLOT_TAKEN_SQL = """
SELECT EXISTS (SELECT 1 FROM recordings o
               WHERE o.work_id = %s AND o.version_type = %s AND o.id <> %s) AS taken
"""


def _retyped_recordings(
    conn: psycopg.Connection[Any], before: Reader, after: Reader
) -> dict[str, dict[str, Any]]:
    """``recordings``: the MusicBrainz recordings a present file's tag names, on a work, whose
    stored version is *before*'s reading of their title and *after*'s differs, so the next
    enrichment upsert (``version_type = EXCLUDED.version_type``) writes the new one.

    A stored version that isn't the before reading (MusicBrainz's live flag, an older reader)
    is not this fix's to change, so it isn't listed.
    """
    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute(_TAGGED_RECORDINGS_SQL).fetchall():
        stored = str(row["version_type"])
        was, now = before(row["title"])[1].value, after(row["title"])[1].value
        if was == now or stored != was:
            continue
        taken = conn.execute(_SLOT_TAKEN_SQL, (row["work_id"], now, row["id"])).fetchone()
        out[str(row["id"])] = {
            "title": row["title"],
            "work_id": row["work_id"],
            "stored": stored,
            "after": now,
            "slot_taken": bool(taken is not None and taken["taken"]),
        }
    # Two going to one slot: whichever upsert comes second fails.
    slots = Counter((entry["work_id"], entry["after"]) for entry in out.values())
    for entry in out.values():
        if slots[(entry["work_id"], entry["after"])] > 1:
            entry["slot_taken"] = True
    return out


def _work_moves(entry: dict[str, Any]) -> bool:
    """Whether a ``differs`` entry's two sides name different works (or new works' titles)."""
    was, now = entry["before"], entry["after"]
    return bool(was["work_id"] != now["work_id"] or was["work_title"] != now["work_title"])


def _summary(
    files: dict[str, dict[str, Any]],
    differs: dict[str, dict[str, Any]],
    recordings: dict[str, dict[str, Any]],
) -> dict[str, int]:
    return {
        "files": len(files),
        "readings_changed": sum(1 for f in files.values() if f["before"] != f["after"]),
        "differs": len(differs),
        "work_differs": sum(1 for entry in differs.values() if _work_moves(entry)),
        "recordings": len(recordings),
        "slot_taken": sum(1 for r in recordings.values() if r["slot_taken"]),
    }


def record(dsn: str, before: Reader, before_root: Path) -> dict[str, Any]:
    """The document, from one read-only snapshot of *dsn*."""
    with gate_common.snapshot_read(dsn) as conn, gate_common.quiet_logging():
        head = gate_common.header(TOOL, FORMAT_VERSION, conn, before=str(before_root))
        files, differs = _file_sections(conn, RepositoryFactory(conn), before)
        recordings = _retyped_recordings(conn, before, extract_version_info)
    return {
        "header": head,
        "files": files,
        "differs": differs,
        "recordings": recordings,
        "summary": _summary(files, differs, recordings),
    }


def _report(doc: dict[str, Any]) -> list[str]:
    """One line per listed file (by path) and per retyped recording, for the console."""
    lines = []
    by_path = sorted(doc["differs"].items(), key=lambda kv: doc["files"][kv[0]]["path"])
    for file_id, entry in by_path:
        f = doc["files"][file_id]
        was, now = entry["before"], entry["after"]
        parts = []
        if _work_moves(entry):
            new = "new work " if now["work_id"] is None else "work "
            parts.append(f"work {was['work_title']!r} -> {new}{now['work_title']!r}")
        if was["version_type"] != now["version_type"]:
            parts.append(f"recording {was['version_type']} -> {now['version_type']}")
        lines.append(f"  file {f['artist']} - {f['title']}: {'; '.join(parts)}")
    for rec_id, rec in sorted(doc["recordings"].items()):
        taken = " (slot taken: its next upsert fails)" if rec["slot_taken"] else ""
        change = f"{rec['stored']} -> {rec['after']}{taken}"
        lines.append(f"  recording {rec_id} {rec['title']}: {change}")
    return lines


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dsn", required=True, help="the gate copy (a bench database)")
    parser.add_argument("--before", required=True, type=Path, help="a checkout of the base")
    parser.add_argument("--out", required=True, type=Path, help="the JSON file to write")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    gate_common.point_at(args.dsn)
    before = load_reader(args.before)
    started = time.perf_counter()
    doc = record(args.dsn, before, args.before)
    gate_common.write_doc(args.out, doc)
    s = doc["summary"]
    print(
        f"{TOOL}: files {s['files']}, readings changed {s['readings_changed']}, "
        f"would group differently {s['differs']} (work {s['work_differs']}), "
        f"recordings retyped {s['recordings']} (slot taken {s['slot_taken']}); "
        f"{time.perf_counter() - started:.1f} s -> {args.out}"
    )
    for line in _report(doc):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

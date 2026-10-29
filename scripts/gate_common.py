"""What the phase 0b gate tools share (SPEC 2026-10-28, gate tools).

- The guards every tool sets before it loads backend code (S10): a DSN without "bench" ends
  the run, streaming is off, the gate has its own Huey file, and the app's settings point at
  the bench database (``bench_enrich.point_app_at``).
- The recorders' file: a header outside the compared payload (tool, format version, the gate
  copy's marker, the commits), sorted JSON.
- The log names' active links, as the air-play snapshot and the golden sample read them.
- The compare (S2): modes, allowlists, work maps and the timing rule.

Nothing here imports backend code at module level: a tool imports this module first, sets its
guards, and only then imports the app.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import structlog
from psycopg.rows import tuple_row

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
for _path in (SCRIPTS, ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# The marker ``gate.py clone`` writes on its copy (COMMENT ON DATABASE), which the recorders
# copy into their headers, so files from two different copies are never compared (E4).
COPY_MARKER = "gate_copy"


# --- the guards (S10) ----------------------------------------------------------------------------


def dbname_of(dsn: str) -> str:
    return str(psycopg.conninfo.conninfo_to_dict(dsn).get("dbname", ""))


def with_dbname(dsn: str, dbname: str) -> str:
    params = psycopg.conninfo.conninfo_to_dict(dsn)
    params["dbname"] = dbname
    return psycopg.conninfo.make_conninfo(**{k: str(v) for k, v in params.items()})


def require_bench(dsn: str) -> str:
    """The DSN's database name; a name without "bench" ends the run (S10)."""
    name = dbname_of(dsn)
    if "bench" not in name:
        raise SystemExit(f"refusing to touch non-bench database {name!r}")
    return name


def set_process_guards() -> None:
    """Streaming off and the gate's own Huey file (``huey_files.py``), for a step that reads
    no settings (a compare, a clone)."""
    os.environ["STREAM_ENABLED"] = "false"
    os.environ["PYTEST_XDIST_WORKER"] = "gate"


def set_guards(dsn: str) -> None:
    """set_process_guards, and the app's settings on *dsn*, a bench database. Safe before any
    backend import."""
    require_bench(dsn)
    set_process_guards()
    os.environ["DATABASE_URL"] = dsn


def guard_from_argv(argv: Sequence[str]) -> None:
    """For a tool run as a script whose module imports backend code: the guards, from its
    ``--dsn``, before those imports run. With no ``--dsn`` (a compare), streaming and the Huey
    file only."""
    set_process_guards()
    for i, arg in enumerate(argv):
        if arg == "--dsn" and i + 1 < len(argv):
            set_guards(argv[i + 1])
        elif arg.startswith("--dsn="):
            set_guards(arg.removeprefix("--dsn="))


def point_at(dsn: str) -> None:
    """The guards, then ``bench_enrich.point_app_at`` (which clears the settings cache)."""
    set_guards(dsn)
    from bench_enrich import point_app_at

    point_app_at(dsn)


# --- the file -----------------------------------------------------------------------------------


def _git(*args: str) -> str | None:
    done = subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False
    )
    out = done.stdout.strip()
    return out if done.returncode == 0 and out else None


def read_copy_marker(conn: psycopg.Connection[Any]) -> dict[str, Any] | None:
    """The gate copy's marker on the connected database, or None when it is not a copy."""
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            """SELECT shobj_description(d.oid, 'pg_database') FROM pg_database d
               WHERE d.datname = current_database()"""
        ).fetchone()
    text = row[0] if row else None
    if not text or not str(text).lstrip().startswith("{"):
        return None
    data = json.loads(text)
    marker = data.get(COPY_MARKER) if isinstance(data, dict) else None
    return dict(marker) if isinstance(marker, dict) else None


def header(
    tool: str, format_version: int, conn: psycopg.Connection[Any], **extra: Any
) -> dict[str, Any]:
    """The header: what is compared (tool, format version, copy, any *extra*) and what is not
    (the commits a base and a branch run differ in)."""
    return {
        "tool": tool,
        "format_version": format_version,
        "copy": read_copy_marker(conn),
        "commit": _git("rev-parse", "HEAD"),
        "master_commit": _git("merge-base", "HEAD", "master"),
        **extra,
    }


def write_doc(path: Path, doc: Mapping[str, Any]) -> None:
    """One sorted JSON file: the same data always gives the same bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def read_doc(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


@contextmanager
def quiet_logging() -> Iterator[None]:
    """Only errors reach the console while a tool runs (the readers warn per play), and
    structlog and the root logger are put back as they were."""
    snapshot = structlog.get_config()
    root = logging.getLogger()
    level = root.level
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))
    root.setLevel(logging.ERROR)
    try:
        yield
    finally:
        structlog.configure(**snapshot)
        root.setLevel(level)


@contextmanager
def snapshot_read(dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """One REPEATABLE READ, READ ONLY transaction on *dsn*, rolled back: every section a run
    records comes from one snapshot, and nothing is written."""
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        try:
            yield conn
        finally:
            conn.rollback()


# --- log name links ------------------------------------------------------------------------------

# Before phase 1a a link is a matches artist row. Read as 1a S2 copies it: the target is a
# catalog id or an MBID; the key is the artist's MBID, else its artists.id (an MBID with no
# catalog row is its own key: 1a gives it a stub of that MBID); origin manual for a manual-tier
# row on a manual_matched name, else legacy; a rejected name's links are not active.
_MATCHES_LINKS_SQL = """
SELECT m.artist_id::text, COALESCE(a.mbid, a.id, m.target_id),
       CASE WHEN m.match_tier = 'manual' AND ba.match_status = 'manual_matched'
            THEN 'manual' ELSE 'legacy' END,
       m.created_at, m.id::text
FROM matches m
JOIN broadcast_artists ba ON ba.id = m.artist_id
LEFT JOIN LATERAL (
    SELECT c.id, c.mbid FROM artists c
    WHERE c.id = m.target_id OR c.mbid = m.target_id
    ORDER BY (c.id = m.target_id) DESC
    LIMIT 1
) a ON true
WHERE m.artist_id IS NOT NULL AND COALESCE(m.target_id, '') <> ''
  AND ba.match_status NOT IN ('manual_rejected', 'auto_rejected')
"""

# From phase 1a on (S1, S2): log_name_links' active rows, with the origin they hold.
_TABLE_LINKS_SQL = """
SELECT l.broadcast_artist_id::text, COALESCE(a.mbid, a.id), l.origin, l.created_at, l.id::text
FROM log_name_links l
JOIN artists a ON a.id = l.artist_id
WHERE l.state = 'active'
"""


def active_links(conn: psycopg.Connection[Any]) -> dict[str, dict[str, str]]:
    """Log name id -> {artist key: origin}: one link per (name, artist), so 1a compares equal.

    A pair held twice collapses as 1a S2 collapses it: manual first, then the earliest.
    """
    with conn.cursor(row_factory=tuple_row) as cur:
        table = cur.execute("SELECT to_regclass('public.log_name_links')").fetchone()
        sql = _TABLE_LINKS_SQL if table and table[0] else _MATCHES_LINKS_SQL
        rows = cur.execute(sql).fetchall()
    links: dict[str, dict[str, str]] = {}
    for name, key, origin, _created, _id in sorted(
        rows, key=lambda r: (r[2] != "manual", r[3], r[4])
    ):
        links.setdefault(name, {}).setdefault(str(key), str(origin))
    return links


# --- compare (S2) --------------------------------------------------------------------------------

MODES = ("exact", "only-down", "additions")
# Timing fails above 2x (S2). Below this many milliseconds of change it never fails: a reader
# that takes a millisecond on a small database doubles on scheduler noise alone.
TIMING_FACTOR = 2.0
TIMING_FLOOR_MS = 5.0
# Header fields two files must share; the commits are outside the compared payload.
_SAME_HEADER = ("tool", "format_version", "copy", "stage")
_HEADER_WORDS = {"format_version": "format version"}


@dataclass(frozen=True)
class CompareSpec:
    """What a tool's compare needs to know about its sections.

    ``work_keyed``: sections keyed by a work id (``<work>`` or ``<work>/<rest>``).
    ``work_valued``: sections whose values hold a work id, in the named field (None: the value
    is the work id). A work map rewrites both before comparing.
    """

    tool: str
    work_keyed: frozenset[str] = frozenset()
    work_valued: Mapping[str, str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class CompareOptions:
    allowed: Mapping[tuple[str, str], str]
    modes: Mapping[str, str]
    work_map: Mapping[str, str]


@dataclass(frozen=True)
class Difference:
    section: str
    key: str
    kind: str
    old: Any
    new: Any

    def entry(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "key": self.key,
            "kind": self.kind,
            "old": self.old,
            "new": self.new,
        }


def add_compare_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("OLD", "NEW"))
    parser.add_argument("--report", type=Path, help="write what the compare found as JSON")
    parser.add_argument("--allow", type=Path, help="allowlist: <section> <key> <reason> lines")
    parser.add_argument("--work-map", type=Path, help="JSON: old work id -> new work id")
    parser.add_argument("--mode", action="extend", nargs="+", default=[], metavar="SECTION=MODE")


def _read_allowlist(path: Path | None) -> dict[tuple[str, str], str] | str:
    """(section, key) -> reason, or why the file is refused."""
    if path is None:
        return {}
    allowed: dict[tuple[str, str], str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            return f"allowlist line {number} has no reason: {text!r}"
        allowed[(parts[0], parts[1])] = parts[2]
    return allowed


def _read_modes(pairs: Sequence[str]) -> dict[str, str] | str:
    modes: dict[str, str] = {}
    for pair in pairs:
        section, _, mode = pair.partition("=")
        if mode not in MODES:
            return f"unknown mode {pair!r} (modes: {', '.join(MODES)})"
        modes[section] = mode
    return modes


def read_options(args: argparse.Namespace) -> CompareOptions | str:
    """The compare's options, or why they are refused."""
    allowed = _read_allowlist(args.allow)
    if isinstance(allowed, str):
        return allowed
    modes = _read_modes(args.mode)
    if isinstance(modes, str):
        return modes
    work_map: dict[str, str] = {}
    if args.work_map is not None:
        if not args.work_map.is_file():
            return f"no work map at {args.work_map}"
        work_map = {str(k): str(v) for k, v in read_doc(args.work_map).items()}
    return CompareOptions(allowed=allowed, modes=modes, work_map=work_map)


def header_refusal(old: Mapping[str, Any], new: Mapping[str, Any]) -> str | None:
    """Why two files are never compared (E4): another tool, format version, copy or stage."""
    for name in _SAME_HEADER:
        a, b = old.get("header", {}).get(name), new.get("header", {}).get(name)
        if a != b:
            return f"different {_HEADER_WORDS.get(name, name)}: {a!r} and {b!r}"
    return None


def _map_key(key: str, work_map: Mapping[str, str]) -> str:
    work, sep, rest = key.partition("/")
    return work_map.get(work, work) + sep + rest


def _map_value(value: Any, work_field: str | None, work_map: Mapping[str, str]) -> Any:
    if work_field is None:
        return work_map.get(value, value) if isinstance(value, str) else value
    if isinstance(value, dict) and isinstance(value.get(work_field), str):
        return {**value, work_field: work_map.get(value[work_field], value[work_field])}
    return value


def _remapped(
    section: str, rows: Mapping[str, Any], spec: CompareSpec, work_map: Mapping[str, str]
) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    """The old rows with the work map applied, and the keys two or more old rows landed on
    (with their values, in old key order)."""
    landed: dict[str, list[Any]] = {}
    for key in sorted(rows):
        value = rows[key]
        if work_map and section in spec.work_valued:
            value = _map_value(value, spec.work_valued[section], work_map)
        new_key = _map_key(key, work_map) if work_map and section in spec.work_keyed else key
        landed.setdefault(new_key, []).append(value)
    single = {k: v[0] for k, v in landed.items() if len(v) == 1}
    merged = {k: v for k, v in landed.items() if len(v) > 1}
    return single, merged


def _section_differences(
    section: str,
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    spec: CompareSpec,
    work_map: Mapping[str, str],
) -> list[Difference]:
    single, merged = _remapped(section, old, spec, work_map)
    found: list[Difference] = []
    for key in sorted(set(single) | set(merged) | set(new)):
        if key in merged:
            found.append(Difference(section, key, "merged", merged[key], new.get(key)))
        elif key not in single:
            found.append(Difference(section, key, "added", None, new[key]))
        elif key not in new:
            found.append(Difference(section, key, "removed", single[key], None))
        elif single[key] != new[key]:
            found.append(Difference(section, key, "changed", single[key], new[key]))
    return found


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _file_of(value: Any) -> Any:
    return value.get("file") if isinstance(value, dict) else value


def _mode_passes(mode: str, difference: Difference) -> bool:
    """only-down: a count may fall or vanish, never rise or appear. additions: none may become
    a file; a file may never become another file or none."""
    kind, old, new = difference.kind, difference.old, difference.new
    if mode == "only-down":
        falls = kind == "changed" and _is_number(old) and _is_number(new) and new < old
        return kind == "removed" or falls
    if mode == "additions":
        return kind in ("added", "changed") and _file_of(old) is None and _file_of(new) is not None
    return False


def _timing_differences(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[Difference]:
    """A reader whose median rose above TIMING_FACTOR times (and by more than the floor)."""
    old_readers = old.get("timing", {}).get("readers", {})
    new_readers = new.get("timing", {}).get("readers", {})
    found: list[Difference] = []
    for reader in sorted(set(old_readers) & set(new_readers)):
        a = old_readers[reader].get("median_ms")
        b = new_readers[reader].get("median_ms")
        if _is_number(a) and _is_number(b) and b > a * TIMING_FACTOR and b - a > TIMING_FLOOR_MS:
            found.append(Difference("timing", reader, "changed", a, b))
    return found


def compare_docs(
    old: Mapping[str, Any], new: Mapping[str, Any], spec: CompareSpec, options: CompareOptions
) -> dict[str, Any]:
    """The report: every difference, failed or allowed (by its section's mode or the
    allowlist), sorted by section and key."""
    old_sections, new_sections = old.get("sections", {}), new.get("sections", {})
    differences: list[Difference] = []
    for section in sorted(set(old_sections) | set(new_sections)):
        differences += _section_differences(
            section,
            old_sections.get(section, {}),
            new_sections.get(section, {}),
            spec,
            options.work_map,
        )
    differences += _timing_differences(old, new)
    failed: list[dict[str, Any]] = []
    allowed: list[dict[str, Any]] = []
    for difference in differences:
        mode = options.modes.get(difference.section, "exact")
        reason = options.allowed.get((difference.section, difference.key))
        if reason is None and _mode_passes(mode, difference):
            reason = f"mode {mode}"
        if reason is None:
            failed.append(difference.entry())
        else:
            allowed.append({**difference.entry(), "reason": reason})
    return {"refused": None, "failed": failed, "allowed": allowed}


def run_compare(args: argparse.Namespace, spec: CompareSpec) -> int:
    """``--compare OLD NEW``: 0 when every difference is allowed, 1 when one is not, 2 when
    the files or the options are refused. Writes ``--report`` and prints a summary."""
    old_path, new_path = args.compare
    old, new = read_doc(old_path), read_doc(new_path)
    options = read_options(args)
    if isinstance(options, str):
        report: dict[str, Any] = {"refused": options, "failed": [], "allowed": []}
    elif (refused := header_refusal(old, new)) is not None:
        report = {"refused": refused, "failed": [], "allowed": []}
    else:
        report = compare_docs(old, new, spec, options)
    if args.report is not None:
        write_doc(args.report, report)
    _print_summary(spec.tool, report)
    if report["refused"] is not None:
        return 2
    return 1 if report["failed"] else 0


def _print_summary(tool: str, report: Mapping[str, Any]) -> None:
    if report["refused"] is not None:
        print(f"{tool} compare refused: {report['refused']}")
        return
    print(f"{tool} compare: {len(report['failed'])} failed, {len(report['allowed'])} allowed")
    for entry in report["failed"][:50]:
        print(f"  FAILED {entry['section']} {entry['key']} {entry['kind']}")
    if len(report["failed"]) > 50:
        print(f"  ... and {len(report['failed']) - 50} more (see the report)")

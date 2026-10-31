r"""Retired names stay retired (artist model phase 0c, SPEC S1, S2; C70, AUD-R045).

ARCHITECTURE.md's "Retired" table holds one row per retired name: the name or pattern, what
replaces it, the phase, the PR, the ruling, and the files allowed to name it (a test that
asserts its absence, say), each an exact repo-relative path. A row's name-or-pattern cell holds
one or more Python regular expressions, each in backticks; a ``|`` inside one is written
``\|``, as in any markdown table cell. An entry is a pattern where a bare name would collide
(``MatchRepository\.get_by_artist``, not ``WorkRepository.get_by_artist``).

S2: no line of a text file in ``backend/``, ``frontend/src/``, ``scripts/`` or ``tests/``
matches a row's pattern, nor any line of ARCHITECTURE.md outside the Retired table, nor of
``.claude/CLAUDE.md`` or ``.claude/rules/*.md``. ``.claude/`` is gitignored, so CI never sees
those two and only local runs (the main checkout, a worktree) check them; a missing one is
skipped. Skipped: migrations, ``backend/db/rollback_*.sql``, the row's allowed files, and this
file, which is the check, as the table is. ``docs/`` is never read: old plans stay as written
history, with a "Retired" note at the top. A hit names the row's ruling.

S1: every name phase 0c retires has a row of phase 0c with a ruling of its own: ACTIVE, saying
"retired in phase 0c, do not recreate", naming what it retires, with its reason, and no longer
pending 0c. No 0c row reaches a name 0c keeps. The services and scripts are gone.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE = ROOT / "ARCHITECTURE.md"
LEDGER = ROOT / "audit" / "rulings.jsonl"
SELF = "tests/test_retired_names.py"

SCANNED = ("backend", "frontend/src", "scripts", "tests")
# Read when present: .claude/ is gitignored, so CI has neither.
LOCAL_ONLY = (".claude/CLAUDE.md", ".claude/rules/*.md")
ALWAYS_SKIPPED = ("backend/db/migrations/*.sql", "backend/db/rollback_*.sql", SELF)
SKIPPED_DIRS = {"__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
COLUMNS = ("name or pattern", "replacement", "phase", "pr", "ruling", "allowed files")

_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_TICKED = re.compile(r"`([^`]+)`")
_RULING_ID = re.compile(r"AUD-R\d{3}")
_PENDING = re.compile(r"; check pending: (?P<phases>[\w., ]+)\.$")


@dataclass(frozen=True)
class RetiredRow:
    patterns: tuple[str, ...]
    phase: str
    ruling: str
    allowed: tuple[str, ...]
    """Exact repo-relative paths allowed to name it."""


@dataclass(frozen=True)
class Hit:
    ruling: str
    pattern: str
    path: str
    line_number: int
    line: str

    def __str__(self) -> str:
        return f"{self.ruling} (`{self.pattern}`): {self.path}:{self.line_number}: {self.line}"


def _cells(line: str) -> list[str]:
    return [c.strip().replace("\\|", "|") for c in _CELL_SPLIT.split(line.strip())[1:-1]]


def _headings(lines: list[str]) -> list[int]:
    """The indices of *lines* that are headings, leaving out ``#`` lines in fenced code."""
    headings: list[int] = []
    fenced = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#"):
            headings.append(i)
    return headings


def retired_table_lines(lines: list[str]) -> range:
    """The indices of *lines* holding the table under the ``## Retired`` heading.

    Empty when there is none. A second table, or a stray row, before the next ``#`` or ``##``
    heading is an error.
    """
    headings = _headings(lines)
    start = next((i for i in headings if re.match(r"## Retired\b", lines[i])), None)
    if start is None:
        return range(0)
    end = next((i for i in headings if i > start and re.match(r"#{1,2} ", lines[i])), None)
    section = range(start + 1, len(lines) if end is None else end)
    rows = [i for i in section if lines[i].strip().startswith("|")]
    if not rows:
        return range(0)
    end_of_table = next((i for i in section if i > rows[0] and i not in rows), section.stop)
    table = range(rows[0], end_of_table)
    stray = [lines[i] for i in rows if i not in table]
    assert stray == [], f"table rows outside the Retired table: {stray}"
    return table


def parse_retired_table(markdown: str) -> list[RetiredRow]:
    """The rows of the table under the ``## Retired`` heading; [] when there is none.

    A row whose cells do not line up with the header is an error, and so is an empty cell
    other than the allowed files.
    """
    lines = markdown.splitlines()
    table = [lines[i] for i in retired_table_lines(lines)]
    if not table:
        return []

    header = [h.lower() for h in _cells(table[0])]
    assert [c for c in COLUMNS if c not in header] == [], f"Retired table columns: {header}"
    rows: list[RetiredRow] = []
    for line in table[2:]:
        cells = dict(zip(header, _cells(line), strict=True))
        assert all(cells[c] for c in COLUMNS if c != "allowed files"), line
        ruling = _RULING_ID.search(cells["ruling"])
        rows.append(
            RetiredRow(
                patterns=tuple(_TICKED.findall(cells["name or pattern"])),
                phase=cells["phase"],
                ruling=ruling.group(0) if ruling else cells["ruling"],
                allowed=tuple(_TICKED.findall(cells["allowed files"])),
            )
        )
    return rows


def retired_rows() -> list[RetiredRow]:
    return parse_retired_table(ARCHITECTURE.read_text(encoding="utf-8"))


def _scanned_texts(root: Path) -> list[tuple[str, str]]:
    """(repo-relative path, text) of every text file under the scanned folders."""
    texts: list[tuple[str, str]] = []
    for top in SCANNED:
        for path in sorted((root / top).rglob("*")):
            rel = path.relative_to(root).as_posix()
            if (
                not path.is_file()
                or SKIPPED_DIRS.intersection(path.relative_to(root).parts)
                or any(fnmatchcase(rel, glob) for glob in ALWAYS_SKIPPED)
            ):
                continue
            data = path.read_bytes()
            if b"\0" not in data:
                texts.append((rel, data.decode("utf-8", errors="replace")))
    for glob in LOCAL_ONLY:
        texts += [
            (path.relative_to(root).as_posix(), path.read_text("utf-8", errors="replace"))
            for path in sorted(root.glob(glob))
            if path.is_file()
        ]
    architecture = root / "ARCHITECTURE.md"
    if architecture.is_file():
        texts.append(("ARCHITECTURE.md", _outside_retired_table(architecture)))
    return texts


def _outside_retired_table(architecture: Path) -> str:
    """ARCHITECTURE.md with the Retired table's lines blanked, so line numbers still hold."""
    lines = architecture.read_text("utf-8", errors="replace").splitlines()
    table = retired_table_lines(lines)
    return "\n".join("" if i in table else line for i, line in enumerate(lines))


def find_hits(root: Path, rows: list[RetiredRow]) -> list[Hit]:
    """Every line under *root*'s scanned folders that names a retired row's pattern."""
    compiled = [(row, [(p, re.compile(p)) for p in row.patterns]) for row in rows]
    hits: list[Hit] = []
    for rel, text in _scanned_texts(root):
        for row, patterns in compiled:
            if rel in row.allowed:
                continue
            for source, pattern in patterns:
                if pattern.search(text) is None:
                    continue
                hits += [
                    Hit(row.ruling, source, rel, number, line.strip())
                    for number, line in enumerate(text.splitlines(), start=1)
                    if pattern.search(line)
                ]
    return hits


def _ledger() -> dict[str, dict[str, Any]]:
    entries = [json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines()]
    return {e["id"]: e for e in entries}


def _pending(ruling: str) -> list[str]:
    match = _PENDING.search(ruling)
    return [p.strip() for p in match["phases"].split(",")] if match else []


# --- S2: the check ---------------------------------------------------------------------------


def test_no_retired_name_is_back() -> None:
    rows = retired_rows()
    assert rows, "ARCHITECTURE.md has no '## Retired' table"
    assert [str(hit) for hit in find_hits(ROOT, rows)] == []


def test_every_row_has_its_ruling_and_real_allowed_files() -> None:
    ledger = _ledger()
    for row in retired_rows():
        assert row.patterns, row
        for pattern in row.patterns:
            re.compile(pattern)
        entry = ledger.get(row.ruling)
        assert entry is not None, f"{row.ruling} is not in audit/rulings.jsonl"
        assert entry["status"] == "ACTIVE", row.ruling
        text = entry["ruling"]
        assert f"retired in phase {row.phase}, do not recreate" in text.lower(), row.ruling
        assert any(re.search(p, text) for p in row.patterns), f"{row.ruling} names none of it"
        assert "tests/test_retired_names.py" in text, row.ruling
        assert entry["rationale"].strip(), row.ruling
        for allowed in row.allowed:
            assert not any(c in allowed for c in "*?["), f"{row.ruling}: {allowed} is a glob"
            assert allowed != "ARCHITECTURE.md", f"{row.ruling}: its table is skipped already"
            assert (ROOT / allowed).is_file(), f"{row.ruling} allows a missing {allowed}"


_SAMPLE_TABLE = r"""
## Retired

Each row is retired (old_helper too); see C70.

| Name or pattern | Replacement | Phase | PR | Ruling | Allowed files |
|---|---|---|---|---|---|
| `old_service` `old_(?:helper\|module)` | none | 0c | #1 | AUD-R901 | `tests/test_absent.py` |
| `MatchRepository\.get_by_artist` | links | 1c | #2 | AUD-R902 | none |

## Next section

| not | retired |
|---|---|
| `old_service` | elsewhere |
"""

_SAMPLE_ROWS = [
    RetiredRow(
        patterns=("old_service", "old_(?:helper|module)"),
        phase="0c",
        ruling="AUD-R901",
        allowed=("tests/test_absent.py",),
    ),
    RetiredRow(
        patterns=(r"MatchRepository\.get_by_artist",), phase="1c", ruling="AUD-R902", allowed=()
    ),
]


def _write_tree(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            (root / rel).write_bytes(content)
        else:
            (root / rel).write_text(content, encoding="utf-8")


def _found(root: Path) -> list[tuple[str, str, int]]:
    return sorted((h.ruling, h.path, h.line_number) for h in find_hits(root, _SAMPLE_ROWS))


def test_table_rows_are_read_with_their_escaped_pipes() -> None:
    assert parse_retired_table(_SAMPLE_TABLE) == _SAMPLE_ROWS
    assert parse_retired_table("# Page\n\nNo retired section.\n") == []
    fenced = "```sh\n# a shell comment, not a heading\n```\n"
    assert parse_retired_table(_SAMPLE_TABLE.replace("Each row", fenced + "Each row")) == (
        _SAMPLE_ROWS
    )


def test_allowed_files_and_rollbacks_skipped(tmp_path: Path) -> None:
    _write_tree(
        tmp_path,
        {
            "backend/services/user.py": "from backend.services.old_service import fold\n",
            "backend/db/migrations/0005_old_service.sql": "CREATE TABLE old_service (id int);\n",
            "backend/db/rollback_0005_old_service.sql": "DROP TABLE old_service;\n",
            "tests/test_absent.py": "assert 'old_service' not in modules\nrepo.get_by_artist\n"
            "MatchRepository.get_by_artist()\n",
            "tests/test_works.py": "WorkRepository.get_by_artist(artist)\n",
            "tests/fixtures/plays.csv": "station,old_service\n",
            "frontend/src/api.ts": "// calls MatchRepository.get_by_artist\n",
            "frontend/src/fine.ts": "export const kept = 1;\n",
            "scripts/tool.py": "x = 1\nimport old_module\n",
            "scripts/__pycache__/tool.cpython-313.pyc": "old_service\n",
            "scripts/audio.flac": b"fLaC\0\0old_service\n",
            SELF: "PHASE_0C_RETIRED = ['old_service']\n",
        },
    )

    assert _found(tmp_path) == [
        ("AUD-R901", "backend/services/user.py", 1),
        ("AUD-R901", "scripts/tool.py", 2),
        ("AUD-R901", "tests/fixtures/plays.csv", 1),
        ("AUD-R902", "frontend/src/api.ts", 1),
        ("AUD-R902", "tests/test_absent.py", 3),
    ]


def test_docs_not_scanned(tmp_path: Path) -> None:
    _write_tree(
        tmp_path,
        {
            "docs/superpowers/plans/old-plan.md": "Run old_service, then old_module.\n",
            "docs/README.md": "MatchRepository.get_by_artist\n",
            "audit/report.md": "old_service was slow\n",
            ".claude/frozen-tests.json": '{"tests/test_old_service.py": "abc"}\n',
            "backend/kept.py": "kept = True\n",
        },
    )

    assert _found(tmp_path) == []


def test_architecture_outside_the_table_and_claude_files_scanned(tmp_path: Path) -> None:
    architecture = "# Architecture\n\nUses old_helper here.\n" + _SAMPLE_TABLE
    _write_tree(
        tmp_path,
        {
            "ARCHITECTURE.md": architecture,
            ".claude/CLAUDE.md": "Never import old_module.\n",
            ".claude/rules/matching.md": "# Rule\nUse MatchRepository.get_by_artist.\n",
            ".claude/rules/notes.txt": "old_service\n",
            ".claude/hooks/guard.py": "OLD = 'old_service'\n",
        },
    )
    lines = architecture.splitlines()

    def line_of(text: str) -> int:
        return next(i for i, line in enumerate(lines, start=1) if text in line)

    assert _found(tmp_path) == [
        ("AUD-R901", ".claude/CLAUDE.md", 1),
        ("AUD-R901", "ARCHITECTURE.md", line_of("Uses old_helper")),
        ("AUD-R901", "ARCHITECTURE.md", line_of("(old_helper too)")),
        ("AUD-R901", "ARCHITECTURE.md", line_of("| `old_service` | elsewhere |")),
        ("AUD-R902", ".claude/rules/matching.md", 2),
    ]


# --- S1: what phase 0c retires and keeps -----------------------------------------------------

# Each must be reached by a phase 0c row: the script-only services, the scripts that only drive
# them, and the repository reads and writes only they call.
PHASE_0C_RETIRED = [
    # work_dedup_service and scripts/dedupe_works.py
    "backend.services.work_dedup_service",
    "from backend.services import work_dedup_service",
    "plan_duplicate_merges",
    "merge_work_group",
    "scripts/dedupe_works.py",
    "scripts.dedupe_works",
    # part_split_service and scripts/split_part_works.py
    "backend.services.part_split_service",
    "from backend.services import part_split_service",
    "plan_part_splits",
    "split_misplaced_parts",
    "PartSplitRepos",
    "PartSplitPlan",
    "scripts/split_part_works.py",
    "scripts.split_part_works",
    # case_duplicate_service and scripts/repair_case_duplicates.py
    "backend.services.case_duplicate_service",
    "from backend.services import case_duplicate_service",
    "plan_case_duplicate_repairs",
    "apply_case_duplicate_repair",
    "CaseDuplicateRepair",
    "scripts/repair_case_duplicates.py",
    "scripts.repair_case_duplicates",
    # work_move_service's repairs and their scripts
    "settle_misplaced_masters",
    "scripts/repair_misplaced_masters.py",
    "scripts.repair_misplaced_masters",
    "realign_match_works",
    "scripts/repair_match_works.py",
    "scripts.repair_match_works",
    # works.merge_into and its _merge_recordings
    "repos.works.merge_into(target, (source,))",
    "work_repo.merge_into(plan.target_id, plan.source_ids)",
    "WorkRepository.merge_into",
    "def merge_into(self, target_id: str, source_ids: tuple[str, ...]) -> None:",
    "self._merge_recordings(target_id, sources)",
    # the footprint, off-file-work and case-duplicate reads only they call
    "list_local_footprints",
    "WorkFootprint",
    "list_off_file_work",
    "MatchOffFileWork",
    "list_work_ids_with_master_off_work",
    "get_case_duplicate_groups",
]

# No phase 0c row may reach any of these: what 0c keeps, and names a later phase builds on.
PHASE_0C_KEPT = [
    "backend.services.work_move_service",
    "move_file_to_work",
    "place_ungrouped_file",
    "WorkMoveRepos",
    "reselect_master_from_files",
    "settle_fold_across_works",
    "missing_file_reconciliation_service",
    "title_part_number",
    "repos.files.merge_into(stale_id, keeper_id)",
    "library_files.merge_into",
    # ARCHITECTURE.md's status-column table, which the scan reads
    "| `track_identities.rejected_file_ids` | Reject / Unmatch (API), library_files.merge_into |",
    "PgLibraryFileRepository(conn).merge_into(source.id, target.id)",
    "def merge_into(self, source_id: UUID, target_id: UUID) -> None:",
    "delete_if_empty",
    "get_candidates_by_normalized_artist",
    "scripts/retitle_works.py",
    "plan_retitle",
    "backend.services.work_curation_service",
    "merge_works",
    "split_work",
    "reassign_file_work",
    "_consolidate_recordings",
    "dropped_override_count",
]

RETIRED_FILES = [
    "backend/services/work_dedup_service.py",
    "backend/services/part_split_service.py",
    "backend/services/case_duplicate_service.py",
    "scripts/dedupe_works.py",
    "scripts/split_part_works.py",
    "scripts/repair_case_duplicates.py",
    "scripts/repair_misplaced_masters.py",
    "scripts/repair_match_works.py",
]

# Kept by 0c; a later phase may retire one through its own row.
KEPT_FILES = [
    "backend/services/work_move_service.py",
    "backend/services/missing_file_reconciliation_service.py",
    "scripts/retitle_works.py",
]


def _reached_by(name: str, rows: list[RetiredRow]) -> list[RetiredRow]:
    return [row for row in rows if any(re.search(p, name) for p in row.patterns)]


def test_phase_0c_retirements_have_rows_and_rulings() -> None:
    rows = retired_rows()
    phase_0c = [row for row in rows if row.phase == "0c"]
    later = [row for row in rows if row.phase != "0c"]

    assert [name for name in PHASE_0C_RETIRED if not _reached_by(name, phase_0c)] == []
    overreach = {name: [r.ruling for r in _reached_by(name, phase_0c)] for name in PHASE_0C_KEPT}
    assert {name: r for name, r in overreach.items() if r} == {}
    assert [row.ruling for row in phase_0c if row.allowed] == []

    rulings = [row.ruling for row in phase_0c]
    assert sorted({r for r in rulings if rulings.count(r) > 1}) == []
    assert all(int(r.removeprefix("AUD-R")) >= 47 for r in rulings), rulings

    assert [path for path in RETIRED_FILES if (ROOT / path).exists()] == []
    missing = [path for path in KEPT_FILES if not (ROOT / path).is_file()]
    module = {path: path.removesuffix(".py").replace("/", ".") for path in missing}
    retired_later = [p for p in missing if _reached_by(p, later) or _reached_by(module[p], later)]
    assert sorted(set(missing) - set(retired_later)) == []


def test_phase_0c_checks_no_longer_pending() -> None:
    ledger = _ledger()
    checked_in_0c = {
        ruling_id
        for ruling_id, entry in ledger.items()
        if "tests/test_retired_names.py (0c)" in entry["ruling"]
    }
    phase_0c = {row.ruling for row in retired_rows() if row.phase == "0c"}

    assert {"AUD-R035", "AUD-R045"} <= checked_in_0c
    assert sorted(phase_0c - checked_in_0c) == []
    assert sorted(r for r in checked_in_0c if "0c" in _pending(ledger[r]["ruling"])) == []

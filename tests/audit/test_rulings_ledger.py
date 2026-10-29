"""The rulings ledger, ``audit/rulings.jsonl`` (artist model phase 0a, SPEC S4, S5, E2).

S5: every line parses; its id is unique; its keys come in the template's order
(``docs/templates/ruling.md``); every ``AUD-R`` id it names in ``refs`` or ``supersedes`` exists
(``AUD-###`` audit findings are not checked). From AUD-R032 on, a ruling ends with its check:
the test files that enforce it, each tagged with the phase that adds it, then the phases still
to land::

    Check: tests/a_test.py (2a), tests/b_test.py (3a); check pending: 2a, 3a.

ARCHITECTURE.md rule 2 says a rule needs a check, and most of these arrive in later phases.
Each phase removes only its own entry from "check pending"; once a phase has left the list,
every file it tagged must exist (E2). With nothing pending, the ruling ends "Check: ... ." alone.

S4: the artist model's rulings AUD-R032 to AUD-R046 (design v3.1 §7) are present, ACTIVE and
dated 2026-10-28, each with its check files, the design ids it settles and Lance's approval;
``supersedes`` names each ruling it replaces in part; and wherever ARCHITECTURE.md cites a
superseded ruling, it cites the new one too.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "audit" / "rulings.jsonl"
ARCHITECTURE = ROOT / "ARCHITECTURE.md"

KEYS = ["id", "status", "date", "refs", "ruling", "rationale", "supersedes"]
FIRST_PHASED_CHECK = 32

_RULING_ID = re.compile(r"AUD-R\d{3}")
_CHECK = re.compile(r"Check: (?P<files>[^;]+)(?:; check pending: (?P<pending>[\w., ]+))?\.")
_CHECK_FILE = re.compile(r"(?P<path>tests/[\w./-]+\.py) \((?P<phase>\d[\w.]*)\)")
# AUD-R018, AUD-R018/R021 (both) and AUD-R015..R031 (the range).
_CITATION = re.compile(r"AUD-R(?P<first>\d{3})(?P<more>(?:/R\d{3})*)(?:\.\.R(?P<last>\d{3}))?")


def ledger() -> list[dict[str, Any]]:
    return [json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines()]


def ruling_number(entry_id: str) -> int | None:
    """The number of an ``AUD-R###`` ruling; None for an ``AUD-###`` audit finding."""
    match = re.fullmatch(r"AUD-R(\d{3})", entry_id)
    return None if match is None else int(match.group(1))


@dataclass(frozen=True)
class Check:
    files: tuple[tuple[str, str], ...]
    """(test file, phase that adds it), in the ruling's order."""
    pending: tuple[str, ...]


def parse_check(ruling: str) -> Check | None:
    """The check *ruling* ends with, or None when it does not end with one in that form."""
    start = ruling.rfind("Check: ")
    match = _CHECK.fullmatch(ruling[start:]) if start >= 0 else None
    if match is None:
        return None
    files = [_CHECK_FILE.fullmatch(item.strip()) for item in match["files"].split(",")]
    if any(f is None for f in files):
        return None
    pending = match["pending"]
    return Check(
        files=tuple((f["path"], f["phase"]) for f in files if f is not None),
        pending=tuple(p.strip() for p in pending.split(",")) if pending else (),
    )


def check_problems(ruling: str, exists: Callable[[str], bool]) -> list[str]:
    """What is wrong with the check *ruling* ends with; *exists* says whether a file exists."""
    check = parse_check(ruling)
    if check is None:
        return ["does not end with 'Check: <test file> (<phase>), ...[; check pending: ...].'"]
    tagged = {phase for _, phase in check.files}
    problems = [f"pending phase {p} tags no check file" for p in check.pending if p not in tagged]
    problems += [
        f"{path} ({phase}) is missing, and {phase} is no longer pending"
        for path, phase in check.files
        if phase not in check.pending and not exists(path)
    ]
    return problems


def _exists(path: str) -> bool:
    return (ROOT / path).is_file()


# --- S5: the whole ledger ----------------------------------------------------------------------


def test_ledger_is_well_formed() -> None:
    lines = LEDGER.read_text(encoding="utf-8").splitlines()
    entries: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        entry = json.loads(line)
        assert isinstance(entry, dict), f"line {number} is not a JSON object"
        assert list(entry) == KEYS, f"line {number} ({entry.get('id')}): keys {list(entry)}"
        entries.append(entry)

    ids = [e["id"] for e in entries]
    assert sorted({i for i in ids if ids.count(i) > 1}) == []
    named = {
        ruling_id
        for e in entries
        for text in [*e["refs"], *e["supersedes"]]
        for ruling_id in cited(text)
    }
    assert sorted(named - set(ids)) == []
    problems = {
        e["id"]: check_problems(e["ruling"], _exists)
        for e in entries
        if (n := ruling_number(e["id"])) is not None and n >= FIRST_PHASED_CHECK
    }
    assert {ruling_id: p for ruling_id, p in problems.items() if p} == {}


def test_pending_marker_list() -> None:
    """E2: files of a pending phase may be missing; once a phase leaves the list, its files
    must exist."""
    only_a = {"tests/a_test.py"}
    two_phases = "A rule. Check: tests/a_test.py (2a), tests/b_test.py (3a), tests/c_test.py (3a)"

    assert check_problems(f"{two_phases}; check pending: 2a, 3a.", lambda _: False) == []
    assert check_problems(f"{two_phases}; check pending: 3a.", only_a.__contains__) == []
    assert check_problems(f"{two_phases}; check pending: 3a.", lambda _: False) == [
        "tests/a_test.py (2a) is missing, and 2a is no longer pending"
    ]
    assert check_problems(f"{two_phases}.", only_a.__contains__) == [
        "tests/b_test.py (3a) is missing, and 3a is no longer pending",
        "tests/c_test.py (3a) is missing, and 3a is no longer pending",
    ]
    assert check_problems(
        "A rule. Check: tests/d_test.py (4.2); check pending: 4.2, 4.3.", lambda _: False
    ) == ["pending phase 4.3 tags no check file"]
    assert parse_check("A rule. Check: tests/d_test.py (4.2); check pending: 4.2.") == Check(
        files=(("tests/d_test.py", "4.2"),), pending=("4.2",)
    )
    assert len(check_problems("A rule with no check.", lambda _: True)) == 1
    assert len(check_problems("A rule. Check: test_d.py (2a).", lambda _: True)) == 1


# --- S4: the artist model's rulings --------------------------------------------------------------

NEW_RULINGS = [f"AUD-R{n:03d}" for n in range(32, 47)]

CHECK_FILES: dict[str, list[tuple[str, str]]] = {
    "AUD-R032": [
        ("tests/integration/test_artist_name_index_pg.py", "2a"),
        ("tests/integration/test_performances_schema_pg.py", "3a"),
        ("tests/integration/test_work_merge_service_pg.py", "3b"),
    ],
    "AUD-R033": [
        ("tests/integration/test_view_version_order_pg.py", "2b"),
        ("tests/integration/test_view_stored_performance_pg.py", "3a"),
        ("tests/integration/test_view_pin_order_pg.py", "3b"),
    ],
    "AUD-R034": [
        ("tests/integration/test_version_entries_schema_pg.py", "2b"),
        ("tests/routers/test_unmapped_versions.py", "2b"),
    ],
    "AUD-R035": [
        ("tests/test_retired_names.py", "0c"),
        ("tests/test_log_name_links_single_writer.py", "1a"),
        ("tests/services/test_tier1_link_read.py", "1a"),
        ("tests/integration/test_work_merge_keeps_linked_artist_pg.py", "1a"),
    ],
    "AUD-R036": [("tests/domain/test_decide.py", "4.2")],
    "AUD-R037": [
        ("tests/domain/test_artist_cascade.py", "1b"),
        ("tests/services/test_rejected_links_stay_rejected.py", "1b"),
        ("tests/routers/test_not_an_artist.py", "1c"),
        ("tests/tasks/test_complete_manual_names.py", "4.4"),
    ],
    "AUD-R038": [
        ("tests/routers/test_missing_matches_by_line_status.py", "1b"),
        ("tests/integration/test_link_demotion_pg.py", "1c"),
        ("tests/services/test_status_from_links.py", "1c"),
        ("tests/integration/test_deferred_retry_retired_pg.py", "4.3"),
    ],
    "AUD-R039": [
        ("tests/integration/test_link_demotion_pg.py", "1c"),
        ("tests/tasks/test_credit_repoint_proposals.py", "3a"),
        ("tests/tasks/test_add_only_pass.py", "4.4"),
    ],
    "AUD-R040": [
        ("tests/test_song_match_single_writer.py", "3a"),
        ("tests/services/test_one_candidate_rule.py", "4.3"),
        ("tests/services/test_rejection_covers_work.py", "4.3"),
        ("tests/services/test_wave_through_artists.py", "4.4"),
    ],
    "AUD-R041": [
        ("tests/services/test_pair_credit.py", "2a"),
        ("tests/integration/test_artist_merge_pg.py", "2a"),
        ("tests/scripts/test_link_harness_library.py", "6"),
    ],
    "AUD-R042": [
        ("tests/integration/test_shared_mb_limiter_pg.py", "2a"),
        ("tests/integration/test_mb_cache_logged_pg.py", "4.1"),
        ("tests/tasks/test_cache_fill.py", "4.1"),
    ],
    "AUD-R043": [
        ("tests/scripts/test_link_harness.py", "4.2"),
        ("tests/services/test_linker_gate.py", "4.2"),
    ],
    "AUD-R044": [
        ("tests/test_migration_lint.py", "0a"),
        ("tests/scripts/test_gate.py", "0b"),
        ("tests/scripts/test_cutover_rehearsal.py", "7"),
    ],
    "AUD-R045": [
        ("tests/test_retired_names.py", "0c"),
        ("tests/tasks/test_empty_work_sweep.py", "3b"),
    ],
    "AUD-R046": [
        ("tests/routers/test_work_versions.py", "2b"),
        ("tests/routers/test_artist_decisions.py", "5"),
    ],
}

# The rulings each new ruling replaces in part, named first in one of its supersedes entries.
SUPERSEDED: dict[str, set[str]] = {
    "AUD-R032": {"AUD-R008"},
    "AUD-R035": {"AUD-R004"},
    "AUD-R036": {"AUD-R022", "AUD-R026"},
    "AUD-R039": {"AUD-R022"},
    "AUD-R040": {"AUD-R024", "AUD-R031"},
    "AUD-R042": {"AUD-R028"},
}

# A design decision whose losing options the rationale must name (SPEC S4).
LOSING_OPTIONS = {"AUD-R032": "M29", "AUD-R033": "M28", "AUD-R040": "M21", "AUD-R041": "M33"}


def _leading_ids(supersedes: list[str]) -> set[str]:
    return {m.group(0) for s in supersedes if (m := _RULING_ID.match(s)) is not None}


def architecture_blocks() -> list[str]:
    """ARCHITECTURE.md as citing units: each heading, paragraph, list item and table row."""
    blocks: list[str] = []
    current: list[str] = []
    for line in ARCHITECTURE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or re.match(r"\s*(?:[-*] |\| |\d+\. |#)", line):
            if current:
                blocks.append(" ".join(current))
            current = []
        if line.strip():
            current.append(line.strip())
    if current:
        blocks.append(" ".join(current))
    return blocks


def cited(block: str) -> set[str]:
    """Every ruling id *block* cites, ranges and slash lists included."""
    numbers: set[int] = set()
    for match in _CITATION.finditer(block):
        first = int(match["first"])
        numbers.add(first)
        numbers.update(int(n) for n in re.findall(r"/R(\d{3})", match["more"]))
        if match["last"]:
            numbers.update(range(first, int(match["last"]) + 1))
    return {f"AUD-R{n:03d}" for n in numbers}


def test_new_rulings_present() -> None:
    by_id = {e["id"]: e for e in ledger()}
    assert [ruling_id for ruling_id in NEW_RULINGS if ruling_id not in by_id] == []

    for ruling_id in NEW_RULINGS:
        entry = by_id[ruling_id]
        assert (entry["status"], entry["date"]) == ("ACTIVE", "2026-10-28"), ruling_id
        check = parse_check(entry["ruling"])
        assert check is not None, ruling_id
        assert list(check.files) == CHECK_FILES[ruling_id], ruling_id
        assert "Lance approved v3.1 on 2026-10-28" in entry["rationale"], ruling_id
        assert re.search(r"\b[MC]\d+\b", entry["rationale"]), ruling_id
        assert _leading_ids(entry["supersedes"]) == SUPERSEDED.get(ruling_id, set()), ruling_id

    for ruling_id, design_id in LOSING_OPTIONS.items():
        assert re.search(rf"\b{design_id}\b", by_id[ruling_id]["rationale"]), ruling_id
    assert any(s.startswith("Invariant 3") for s in by_id["AUD-R036"]["supersedes"])

    # Every ARCHITECTURE.md citation of a superseded ruling also cites the new one.
    uncited = [
        (new, old, block[:80])
        for new, olds in SUPERSEDED.items()
        for old in sorted(olds)
        for block in architecture_blocks()
        if old in cited(block) and new not in cited(block)
    ]

    assert uncited == []


def test_citations_are_read_with_their_ranges_and_lists() -> None:
    assert cited("AUD-R018/R021 and AUD-R012 (1)") == {"AUD-R018", "AUD-R021", "AUD-R012"}
    assert cited("(AUD-R030..R032 ACTIVE)") == {"AUD-R030", "AUD-R031", "AUD-R032"}

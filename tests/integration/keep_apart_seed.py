"""Seeds and reads for Lance's "Keep apart" marks (artist model phase 3b PR 5c; SPEC D40,
D55-D58; scope check Q3, Q4, Q6-Q9, Q11; AUD-R089).

Plain SQL on the test's own connection, built on the locked ``credit_picks_seed`` and
``work_merge_seed``. A mark is one row of ``work_keep_apart``: its two work ids in plain string
order (the lower first, as the table's ``COLLATE "C"`` check wants), when, and a note.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from tests.integration import credit_picks_seed as cp
from tests.integration import work_merge_seed as seed
from tests.integration.credit_picks_seed import Conn

TABLE = "work_keep_apart"
VERSION = "0056_work_keep_apart"

# The notes each writer sets (Q8): there is no input box.
MERGE_LIST_NOTE = "Kept apart on the merge list"


def split_note(file_name: str) -> str:
    return f"Split off {file_name}"


def reassign_note(file_name: str, title: str) -> str:
    return f"Reassigned {file_name} to {title}"


def ordered(first: str, second: str) -> tuple[str, str]:
    """The pair as the table stores it: plain string (code point) order."""
    a, b = sorted((first, second))
    return a, b


def keep(
    conn: Conn, first: str, second: str, *, note: str = "", at: datetime | None = None
) -> tuple[str, str]:
    """Keep two works apart by plain SQL; the pair as stored."""
    a, b = ordered(first, second)
    conn.execute(
        f"""INSERT INTO {TABLE} (first_work_id, second_work_id, note, kept_at)
            VALUES (%s, %s, %s, COALESCE(%s, now()))""",
        (a, b, note, at),
    )
    return a, b


def kept(conn: Conn) -> set[tuple[str, str]]:
    """Every pair kept apart, as stored."""
    rows = conn.execute(f"SELECT first_work_id, second_work_id FROM {TABLE}").fetchall()
    return {(str(r["first_work_id"]), str(r["second_work_id"])) for r in rows}


def marks(conn: Conn) -> dict[tuple[str, str], dict[str, Any]]:
    """Every mark by its pair: its note and when."""
    rows = conn.execute(
        f"SELECT first_work_id, second_work_id, note, kept_at FROM {TABLE}"
    ).fetchall()
    return {
        (str(r["first_work_id"]), str(r["second_work_id"])): {
            "note": r["note"],
            "kept_at": r["kept_at"],
        }
        for r in rows
    }


def file_name(conn: Conn, file_id: UUID) -> str:
    """The last part of a file's path, either separator."""
    row = conn.execute("SELECT file_path FROM library_files WHERE id = %s", (file_id,)).fetchone()
    assert row is not None
    return re.split(r"[\\/]", str(row["file_path"]))[-1]


def redirect(conn: Conn, old_id: str, new_id: str) -> None:
    """*old_id* merged into *new_id* (a logged merge and its redirect), by plain SQL."""
    row = conn.execute(
        """INSERT INTO work_merges (target_id, source_ids, reason, approvals, status)
           VALUES (%s, %s, 'library', '[]'::jsonb, 'merged') RETURNING id""",
        (new_id, [old_id]),
    ).fetchone()
    assert row is not None
    conn.execute(
        "INSERT INTO work_redirects (old_id, new_id, merge_id) VALUES (%s, %s, %s)",
        (old_id, new_id, row["id"]),
    )


class ThreeTakes:
    """Bon Jovi's "Always" filed three times, each work with one copy (its master) credited to
    the act: ``studio`` (heard 5 times), ``live`` (heard once) and ``demo``. Unmarked, the
    planner joins all three (same title key, shared artist)."""

    def __init__(self, conn: Conn, title: str = "Always") -> None:
        self.act = cp.artist(conn, "Bon Jovi")
        at = seed.station(conn)
        self.studio, self.studio_copy = self._take(conn, title)
        self.live, self.live_copy = self._take(conn, title)
        self.demo, self.demo_copy = self._take(conn, title)
        seed.heard(conn, self.studio_copy, at, title=title, times=5)
        seed.heard(conn, self.live_copy, at, title=title)

    def _take(self, conn: Conn, title: str) -> tuple[str, UUID]:
        work_id = cp.work(conn, title, self.act)
        copy = cp.copy(conn, work_id, title=title)
        cp.performance(conn, work_id, [copy], credit=[self.act])
        seed.master(conn, work_id, copy)
        return work_id, copy

    @property
    def works(self) -> list[str]:
        return [self.studio, self.live, self.demo]

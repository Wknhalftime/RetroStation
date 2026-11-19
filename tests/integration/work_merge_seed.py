"""Seeds and reads for the guarded work merge (artist model phase 3b PR 3; SPEC S3, S4, S10, S12,
D2-D4, D13, D14, D22, D23; AUD-R079).

Built on the locked ``credit_picks_seed``: each helper writes one thing by plain SQL. A merge
goes through the guard, ``work_merge_service.guarded_merge``, on the test's own connection, so
seeds need no commit (the router tests commit theirs). Air play is read only through
``play_file_resolution``.

Two worlds:

- :class:`TwoWorks`: one act's song filed twice. The target's master is a flac, the source's
  an mp3, both automatic, so the merged work's master is the target's (D8: re-picked from every
  present file). Every copy is credited to the act, so the source's copies join the target's
  performance and the merged work holds one act.
- :class:`Cover`: "Lady Marmalade" by LaBelle (the target, its master a flac) and by Christina
  Aguilera (the source, an mp3): two acts, each with its own performance and linked log name.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from backend.domain.work_merges import (
    MergeApproval,
    MergeChange,
    MergeDone,
    MergeReason,
    MergeRefused,
    WorkMergeGroup,
)
from backend.services.repository_factory import RepositoryFactory, work_merge_repos
from backend.services.work_merge_service import guarded_merge
from tests.integration import credit_picks_seed as cp
from tests.integration.credit_picks_seed import Conn

ROOT = Path(__file__).resolve().parents[2]
RESTORE = ROOT / "backend" / "db" / "rollback_work_merges.sql"

# The tables a merge may write, with each one's key: what S10's rollback map must hold and
# S10's restore must put back. The merge log's own tables are not among them.
KEYS: dict[str, tuple[str, ...]] = {
    "artists": ("id",),
    "artist_names": ("id",),
    "works": ("id",),
    "recordings": ("id",),
    "library_files": ("id",),
    "performances": ("id",),
    "performance_credits": ("performance_id", "position"),
    "song_masters": ("id",),
    "format_overrides": ("id",),
    "version_entry_states": ("work_id", "version_text"),
    "matches": ("id",),
    "credit_pick_proposals": ("id",),
}

type Snapshot = dict[str, dict[str, dict[str, Any]]]
"""Each table's rows as JSON objects, by key (its key columns, JSON-encoded)."""


# --- the catalog ----------------------------------------------------------------------------------


def master(conn: Conn, work_id: str, file_id: UUID, method: str = "auto") -> None:
    """The work's song master, picked by *method* (``auto`` or ``manual``)."""
    conn.execute(
        """INSERT INTO song_masters (work_id, preferred_file_id, selection_method)
           VALUES (%s, %s, %s)
           ON CONFLICT (work_id) DO UPDATE
           SET preferred_file_id = EXCLUDED.preferred_file_id,
               selection_method = EXCLUDED.selection_method""",
        (work_id, file_id, method),
    )


def master_of(conn: Conn, work_id: str) -> tuple[UUID, str] | None:
    """(file, method) of the work's master."""
    row = conn.execute(
        "SELECT preferred_file_id, selection_method FROM song_masters WHERE work_id = %s",
        (work_id,),
    ).fetchone()
    return None if row is None else (UUID(str(row["preferred_file_id"])), row["selection_method"])


def work_exists(conn: Conn, work_id: str) -> bool:
    return conn.execute("SELECT 1 FROM works WHERE id = %s", (work_id,)).fetchone() is not None


def work_of(conn: Conn, file_id: UUID) -> str | None:
    row = conn.execute("SELECT work_id FROM library_files WHERE id = %s", (file_id,)).fetchone()
    assert row is not None
    return None if row["work_id"] is None else str(row["work_id"])


def performance_of(conn: Conn, file_id: UUID) -> UUID | None:
    row = conn.execute(
        "SELECT performance_id FROM library_files WHERE id = %s", (file_id,)
    ).fetchone()
    assert row is not None
    return None if row["performance_id"] is None else UUID(str(row["performance_id"]))


def missing(conn: Conn, file_id: UUID) -> None:
    """The file went missing from disk."""
    conn.execute(
        """UPDATE library_files SET file_status = 'missing', missing_since = now()
           WHERE id = %s""",
        (file_id,),
    )


# --- stations, lines and plays --------------------------------------------------------------------


def station(conn: Conn, format_name: str | None = None) -> UUID:
    """A station of its own (unique call letters); with no format, no format override reaches
    its lines."""
    row = conn.execute(
        "INSERT INTO stations (call_letters, format_name) VALUES (%s, %s) RETURNING id",
        (f"K{uuid4().hex[:6].upper()}", format_name),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def heard(
    conn: Conn,
    file_id: UUID,
    at: UUID,
    *,
    title: str = "Lady Marmalade",
    name: UUID | None = None,
    times: int = 1,
) -> UUID:
    """A line under the log name *name* (a fresh unlinked one unless given), its one match on
    *file_id*, played *times* times at the station *at*."""
    identity_id = cp.line(conn, title, file_id, name=name)
    cp.plays(conn, identity_id, times, station_id=at)
    return identity_id


def also_heard(conn: Conn, identity_id: UUID, at: UUID, times: int = 1) -> None:
    """More plays of the line, at the station *at*."""
    cp.plays(conn, identity_id, times, station_id=at)


def playing(
    conn: Conn, identity_id: UUID, at: UUID | None = None
) -> set[tuple[UUID | None, str | None]]:
    """(file_id, source) of each play of the line (at the station *at*, if given), as the
    air-play view resolves it."""
    rows = conn.execute(
        """SELECT pfr.file_id, pfr.source FROM play_file_resolution pfr
           JOIN play_events pe ON pe.id = pfr.play_event_id
           WHERE pe.identity_id = %s AND (%s::uuid IS NULL OR pfr.station_id = %s::uuid)""",
        (identity_id, at, at),
    ).fetchall()
    return {(None if r["file_id"] is None else UUID(str(r["file_id"])), r["source"]) for r in rows}


def statuses(conn: Conn, identity_id: UUID) -> set[str | None]:
    """The ``file_status`` the view reports for each play of the line."""
    rows = conn.execute(
        """SELECT pfr.file_status FROM play_file_resolution pfr
           JOIN play_events pe ON pe.id = pfr.play_event_id
           WHERE pe.identity_id = %s""",
        (identity_id,),
    ).fetchall()
    return {r["file_status"] for r in rows}


def pin_of(conn: Conn, identity_id: UUID) -> UUID | None:
    """The file the line's best match pins (``matches.pinned_file_id``), or None."""
    row = conn.execute(
        "SELECT pinned_file_id FROM matches WHERE id = %s", (cp.best_match(conn, identity_id),)
    ).fetchone()
    assert row is not None
    return None if row["pinned_file_id"] is None else UUID(str(row["pinned_file_id"]))


def pin(conn: Conn, identity_id: UUID, file_id: UUID) -> None:
    """Seed a pin on the line's best match, as a merge would leave it."""
    conn.execute(
        "UPDATE matches SET pinned_file_id = %s WHERE id = %s",
        (file_id, cp.best_match(conn, identity_id)),
    )


def stored_of(conn: Conn, identity_id: UUID) -> UUID | None:
    """The performance the line's best match stores, or None."""
    row = conn.execute(
        "SELECT performance_id FROM matches WHERE id = %s", (cp.best_match(conn, identity_id),)
    ).fetchone()
    assert row is not None
    return None if row["performance_id"] is None else UUID(str(row["performance_id"]))


# --- the guarded merge ----------------------------------------------------------------------------


def merge(
    conn: Conn,
    target: str,
    sources: Sequence[str],
    *,
    reason: MergeReason = MergeReason.LIBRARY,
    approvals: Iterable[MergeApproval] = (),
) -> MergeDone | MergeRefused:
    """The guarded merge of *sources* into *target*, on the test's connection."""
    group = WorkMergeGroup(
        target_id=target,
        source_ids=tuple(sources),
        reason=reason,
        approvals=frozenset(approvals),
    )
    return guarded_merge(group, work_merge_repos(RepositoryFactory(conn)))


def merged(conn: Conn, target: str, sources: Sequence[str], **kwargs: Any) -> MergeDone:
    """:func:`merge`, which must merge."""
    outcome = merge(conn, target, sources, **kwargs)
    assert isinstance(outcome, MergeDone), outcome
    return outcome


def refused(conn: Conn, target: str, sources: Sequence[str], **kwargs: Any) -> MergeRefused:
    """:func:`merge`, which must be refused for changes awaiting approval."""
    outcome = merge(conn, target, sources, **kwargs)
    assert isinstance(outcome, MergeRefused), outcome
    return outcome


def change_keys(
    changes: Iterable[MergeChange],
) -> set[tuple[UUID, UUID, UUID | None, UUID | None]]:
    """(station, line, file before, file after) of each change."""
    return {(c.station_id, c.identity_id, c.from_file_id, c.to_file_id) for c in changes}


def approving(changes: Iterable[MergeChange]) -> list[MergeApproval]:
    """Lance's approval of each change: its (station, line, new file)."""
    return [MergeApproval(c.station_id, c.identity_id, c.to_file_id) for c in changes]


def merges_of(conn: Conn, target: str) -> list[dict[str, Any]]:
    """The merge log's rows naming *target* as the merged work, oldest first."""
    return list(
        conn.execute(
            """SELECT id, target_id, source_ids, reason, approvals, status, error
               FROM work_merges WHERE target_id = %s ORDER BY logged_at, id""",
            (target,),
        ).fetchall()
    )


def redirects(conn: Conn, work_ids: Iterable[str]) -> dict[str, str]:
    """Where each of *work_ids* redirects (only those that do)."""
    rows = conn.execute(
        "SELECT old_id, new_id FROM work_redirects WHERE old_id = ANY(%s)", (list(work_ids),)
    ).fetchall()
    return {str(r["old_id"]): str(r["new_id"]) for r in rows}


# --- S10: the rollback map and the restore --------------------------------------------------------


def fresh_log(conn: Conn) -> None:
    """Empty the merge log: the restore restores every logged merge, and the tests' cleanup
    (TRUNCATE ... CASCADE from the catalog) needn't reach the log's tables."""
    conn.execute("TRUNCATE work_merges, work_merge_rows, work_redirects CASCADE")


def snapshot(conn: Conn) -> Snapshot:
    """Every row of every table a merge may write, by key."""
    found: Snapshot = {}
    for table, key in KEYS.items():
        rows = conn.execute(f"SELECT to_jsonb(t) AS r FROM {table} t").fetchall()
        found[table] = {json.dumps([r["r"][k] for k in key]): r["r"] for r in rows}
    return found


def map_of(conn: Conn, merge_id: UUID) -> list[dict[str, Any]]:
    """The rollback map of one merge: (table, row as it was, NULL for a row it inserted)."""
    return list(
        conn.execute(
            "SELECT table_name, before FROM work_merge_rows WHERE merge_id = %s",
            (merge_id,),
        ).fetchall()
    )


def restore(conn: Conn) -> None:
    """Run S10's restore, ``backend/db/rollback_work_merges.sql``, on the test's connection."""
    conn.execute(RESTORE.read_text(encoding="utf-8"))


# --- the worlds -----------------------------------------------------------------------------------


class TwoWorks:
    """Bon Jovi's "Always" filed twice: the target (its master ``target_master``, a flac) and
    the source (its master ``source_master`` and another copy ``source_copy``, both mp3), both
    masters automatic. Every copy is credited to Bon Jovi."""

    def __init__(self, conn: Conn, title: str = "Always") -> None:
        self.title = title
        self.act = cp.artist(conn, "Bon Jovi")
        self.target = cp.work(conn, title, self.act)
        self.target_master = cp.copy(conn, self.target, fmt="flac", title=title)
        self.target_performance = cp.performance(
            conn, self.target, [self.target_master], credit=[self.act]
        )
        master(conn, self.target, self.target_master)
        self.source = cp.work(conn, title, self.act)
        self.source_master = cp.copy(conn, self.source, fmt="mp3", title=title)
        self.source_copy = cp.copy(conn, self.source, fmt="mp3", title=title)
        self.source_performance = cp.performance(
            conn, self.source, [self.source_master, self.source_copy], credit=[self.act]
        )
        master(conn, self.source, self.source_master)

    def source_file(self, conn: Conn, fmt: str = "mp3") -> UUID:
        """Another copy of the source, in its performance."""
        file_id = cp.copy(conn, self.source, fmt=fmt, title=self.title)
        conn.execute(
            "UPDATE library_files SET performance_id = %s WHERE id = %s",
            (self.source_performance, file_id),
        )
        return file_id


class Cover:
    """ "Lady Marmalade": LaBelle's work (the target; its master ``labelle_master``, a flac, in
    the performance ``labelle``) and Christina Aguilera's (the source; its master
    ``aguilera_master``, an mp3, in ``aguilera``), both masters automatic. The log names
    "LABELLE" and "CHRISTINA AGUILERA" are linked to their acts."""

    def __init__(self, conn: Conn) -> None:
        self.labelle_act = cp.artist(conn, "LaBelle")
        self.aguilera_act = cp.artist(conn, "Christina Aguilera")
        self.target = cp.work(conn, "Lady Marmalade", self.labelle_act)
        self.labelle_master = cp.copy(conn, self.target, fmt="flac", title="Lady Marmalade")
        self.labelle = cp.performance(
            conn, self.target, [self.labelle_master], credit=[self.labelle_act]
        )
        master(conn, self.target, self.labelle_master)
        self.source = cp.work(conn, "Lady Marmalade", self.aguilera_act)
        self.aguilera_master = cp.copy(conn, self.source, fmt="mp3", title="Lady Marmalade")
        self.aguilera = cp.performance(
            conn, self.source, [self.aguilera_master], credit=[self.aguilera_act]
        )
        master(conn, self.source, self.aguilera_master)
        self.labelle_name = cp.log_name(conn, "LABELLE", self.labelle_act)
        self.aguilera_name = cp.log_name(conn, "CHRISTINA AGUILERA", self.aguilera_act)

    def copy_of(self, conn: Conn, performance_id: UUID, work_id: str, fmt: str = "mp3") -> UUID:
        """Another copy in *performance_id* (a performance of *work_id*)."""
        file_id = cp.copy(conn, work_id, fmt=fmt, title="Lady Marmalade")
        conn.execute(
            "UPDATE library_files SET performance_id = %s WHERE id = %s",
            (performance_id, file_id),
        )
        return file_id

    def labelle_line(self, conn: Conn, at: UUID, title: str = "Lady Marmalade") -> UUID:
        return heard(conn, self.labelle_master, at, title=title, name=self.labelle_name)

    def aguilera_line(self, conn: Conn, at: UUID, *, linked: bool = True) -> UUID:
        name = self.aguilera_name if linked else None
        return heard(conn, self.aguilera_master, at, name=name)

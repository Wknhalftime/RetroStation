"""PostgreSQL adapter for the Credit picks port (phase 3a PR 3, SPEC S8, S9)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.db.repositories.artist_merges import MERGE_ROOTS_CTE
from backend.domain.catalog import CatalogStorageError
from backend.domain.credit_picks import (
    LineCredit,
    LineNow,
    Proposal,
    ProposalAnswer,
    ProposalKind,
    ProposalState,
    WorkCredits,
)
from backend.domain.performances import CreditedPerformance, PerformanceReason
from backend.repositories.credit_picks import CreditPickRepository, FileShown, LineShown

# The view's matched line: auto or manual matched, its best match with a file, that file's
# work, else its recording's.
_BEST_MATCH = """LATERAL (
    SELECT m.id, m.library_file_id, m.performance_id
      FROM matches m
     WHERE m.identity_id = ti.id
       AND m.library_file_id IS NOT NULL
       AND ti.match_status IN ('auto_matched', 'manual_matched')
     ORDER BY m.confidence_score DESC, m.created_at, m.id
     LIMIT 1
)"""

_LINES_TO_PROPOSE = f"""
SELECT ti.id AS identity_id, best.id AS match_id, best.performance_id AS stored_id,
       COALESCE(f.work_id, r.work_id) AS work_id, ti.version_text,
       ARRAY(SELECT l.artist_id FROM log_name_links l
              WHERE l.broadcast_artist_id = ti.broadcast_artist_id AND l.state = 'active')
           AS artists
  FROM track_identities ti
  CROSS JOIN {_BEST_MATCH} best
  JOIN library_files f ON f.id = best.library_file_id
  LEFT JOIN recordings r ON r.id = f.recording_id
 WHERE COALESCE(f.work_id, r.work_id) IS NOT NULL
   AND EXISTS (SELECT 1 FROM log_name_links l
                WHERE l.broadcast_artist_id = ti.broadcast_artist_id AND l.state = 'active')
"""

# A credit names its artist by id, else the artist holding its MBID (D1), else the MBID.
_PERFORMANCE_ARTISTS = """
SELECT p.id, p.work_id,
       array_remove(array_agg(DISTINCT COALESCE(pc.artist_id, a.id, 'mbid:' || pc.mbid)), NULL)
           AS artists
  FROM performances p
  LEFT JOIN performance_credits pc ON pc.performance_id = p.id
  LEFT JOIN artists a ON pc.artist_id IS NULL AND a.mbid = pc.mbid
 WHERE p.work_id = ANY(%s)
 GROUP BY p.id, p.work_id
"""

_WORKS = """
SELECT w.id, w.artist_id, mf.performance_id AS default_id
  FROM works w
  LEFT JOIN song_masters sm ON sm.work_id = w.id
  LEFT JOIN library_files mf ON mf.id = sm.preferred_file_id
 WHERE w.id = ANY(%s)
"""

_PROPOSAL_COLUMNS = """id, identity_id, match_id, performance_id, kind, reason, candidates, state"""

_LINES_NOW = f"""
SELECT ti.id AS identity_id, best.id AS match_id, COALESCE(f.work_id, r.work_id) AS work_id,
       mf.performance_id AS default_id
  FROM track_identities ti
  LEFT JOIN {_BEST_MATCH} best ON true
  LEFT JOIN library_files f ON f.id = best.library_file_id
  LEFT JOIN recordings r ON r.id = f.recording_id
  LEFT JOIN song_masters sm ON sm.work_id = COALESCE(f.work_id, r.work_id)
  LEFT JOIN library_files mf ON mf.id = sm.preferred_file_id
 WHERE ti.id = ANY(%s)
"""

_LINES_SHOWN = f"""
SELECT ti.id AS identity_id, ti.original_title AS title, ba.original_name AS artist_name,
       (SELECT count(*) FROM play_events pe WHERE pe.identity_id = ti.id) AS plays,
       w.id AS work_id, w.title AS work_title,
       latest.file_id AS now_file_id, latest.source AS now_source
  FROM track_identities ti
  JOIN broadcast_artists ba ON ba.id = ti.broadcast_artist_id
  LEFT JOIN {_BEST_MATCH} best ON true
  LEFT JOIN library_files f ON f.id = best.library_file_id
  LEFT JOIN recordings r ON r.id = f.recording_id
  LEFT JOIN works w ON w.id = COALESCE(f.work_id, r.work_id)
  LEFT JOIN LATERAL (
      SELECT pfr.file_id, pfr.source
        FROM play_events pe
       CROSS JOIN LATERAL (
           SELECT v.file_id, v.source FROM play_file_resolution v
            WHERE v.play_event_id = pe.id OFFSET 0
       ) pfr
       WHERE pe.identity_id = ti.id
       ORDER BY pe.played_at DESC, pe.id DESC
       LIMIT 1
  ) latest ON true
 WHERE ti.id = ANY(%s)
"""

_CREDIT_NAMES = """
SELECT pc.performance_id, COALESCE(pc.credited_name, a.name, held.name, pc.mbid) AS name
  FROM performance_credits pc
  LEFT JOIN artists a ON a.id = pc.artist_id
  LEFT JOIN artists held ON pc.artist_id IS NULL AND held.mbid = pc.mbid
 WHERE pc.performance_id = ANY(%s)
 ORDER BY pc.performance_id, pc.position
"""


def _uuid(value: Any) -> UUID | None:
    return None if value is None else UUID(str(value))


def _proposal(row: DictRow) -> Proposal:
    return Proposal(
        id=UUID(str(row["id"])),
        identity_id=UUID(str(row["identity_id"])),
        match_id=_uuid(row["match_id"]),
        kind=ProposalKind(row["kind"]),
        reason=PerformanceReason(row["reason"]),
        performance_id=_uuid(row["performance_id"]),
        candidates=tuple(sorted(UUID(str(c)) for c in row["candidates"])),
        state=ProposalState(row["state"]),
    )


@translate_lost_connection(CatalogStorageError)
class PgCreditPickRepository(CreditPickRepository):
    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self._conn = conn

    def lines_to_propose(self) -> list[LineCredit]:
        rows = self._conn.execute(_LINES_TO_PROPOSE).fetchall()
        return [
            LineCredit(
                identity_id=UUID(str(r["identity_id"])),
                match_id=UUID(str(r["match_id"])),
                stored_performance_id=_uuid(r["stored_id"]),
                work_id=str(r["work_id"]),
                version_text=r["version_text"],
                artists=frozenset(str(a) for a in r["artists"]),
            )
            for r in rows
        ]

    def work_credits(self, work_ids: Sequence[str]) -> dict[str, WorkCredits]:
        ids = list(work_ids)
        performances: dict[str, list[CreditedPerformance]] = {w: [] for w in ids}
        for r in self._conn.execute(_PERFORMANCE_ARTISTS, (ids,)).fetchall():
            performances[str(r["work_id"])].append(
                CreditedPerformance(
                    id=UUID(str(r["id"])), artists=frozenset(str(a) for a in r["artists"])
                )
            )
        return {
            str(r["id"]): WorkCredits(
                work_id=str(r["id"]),
                artist_id=None if r["artist_id"] is None else str(r["artist_id"]),
                default_performance_id=_uuid(r["default_id"]),
                performances=tuple(performances[str(r["id"])]),
            )
            for r in self._conn.execute(_WORKS, (ids,)).fetchall()
        }

    def primaries(self, artist_ids: Iterable[str]) -> dict[str, str]:
        rows = self._conn.execute(
            f"""WITH RECURSIVE {MERGE_ROOTS_CTE}
                SELECT artist_id, primary_id FROM merge_roots WHERE artist_id = ANY(%s)""",
            (sorted(set(artist_ids)),),
        ).fetchall()
        return {str(r["artist_id"]): str(r["primary_id"]) for r in rows}

    def all_proposals(self) -> list[Proposal]:
        rows = self._conn.execute(
            f"SELECT {_PROPOSAL_COLUMNS} FROM credit_pick_proposals"
        ).fetchall()
        return [_proposal(r) for r in rows]

    def proposals(self, ids: Sequence[UUID]) -> dict[UUID, Proposal]:
        rows = self._conn.execute(
            f"""SELECT {_PROPOSAL_COLUMNS} FROM credit_pick_proposals
                WHERE id = ANY(%s) ORDER BY id FOR UPDATE""",
            (list(ids),),
        ).fetchall()
        return {p.id: p for p in map(_proposal, rows)}

    def listed(self) -> list[Proposal]:
        rows = self._conn.execute(
            f"""SELECT {_PROPOSAL_COLUMNS} FROM credit_pick_proposals
                WHERE state = 'proposed' OR (state = 'picked' AND kind <> 'credit')"""
        ).fetchall()
        return [_proposal(r) for r in rows]

    def insert(self, identity_id: UUID, answer: ProposalAnswer) -> UUID:
        row = self._conn.execute(
            """INSERT INTO credit_pick_proposals
                   (identity_id, match_id, performance_id, kind, reason, candidates)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                identity_id,
                answer.match_id,
                answer.performance_id,
                answer.kind.value,
                answer.reason.value,
                list(answer.candidates),
            ),
        ).fetchone()
        if row is None:
            raise CatalogStorageError("INSERT INTO credit_pick_proposals returned no row")
        return UUID(str(row["id"]))

    def delete(self, ids: Sequence[UUID]) -> None:
        if ids:
            self._conn.execute("DELETE FROM credit_pick_proposals WHERE id = ANY(%s)", (list(ids),))

    def decide(
        self, ids: Sequence[UUID], state: ProposalState, performance_id: UUID | None = None
    ) -> None:
        if not ids:
            return
        if state == ProposalState.PICKED:
            self._conn.execute(
                """UPDATE credit_pick_proposals
                   SET state = %s, decided_at = now(), performance_id = %s
                   WHERE id = ANY(%s)""",
                (state.value, performance_id, list(ids)),
            )
            return
        self._conn.execute(
            """UPDATE credit_pick_proposals SET state = %s, decided_at = now()
               WHERE id = ANY(%s)""",
            (state.value, list(ids)),
        )

    def lines_now(self, identity_ids: Sequence[UUID]) -> dict[UUID, LineNow]:
        rows = self._conn.execute(_LINES_NOW, (list(identity_ids),)).fetchall()
        return {
            UUID(str(r["identity_id"])): LineNow(
                identity_id=UUID(str(r["identity_id"])),
                match_id=_uuid(r["match_id"]),
                work_id=None if r["work_id"] is None else str(r["work_id"]),
                default_performance_id=_uuid(r["default_id"]),
            )
            for r in rows
        }

    def performance_works(self, performance_ids: Iterable[UUID]) -> dict[UUID, str]:
        rows = self._conn.execute(
            "SELECT id, work_id FROM performances WHERE id = ANY(%s)",
            (sorted(set(performance_ids)),),
        ).fetchall()
        return {UUID(str(r["id"])): str(r["work_id"]) for r in rows}

    def lines_shown(self, identity_ids: Sequence[UUID]) -> dict[UUID, LineShown]:
        rows = self._conn.execute(_LINES_SHOWN, (list(identity_ids),)).fetchall()
        return {
            UUID(str(r["identity_id"])): LineShown(
                identity_id=UUID(str(r["identity_id"])),
                title=str(r["title"]),
                artist_name=str(r["artist_name"]),
                plays=int(r["plays"]),
                work_id=None if r["work_id"] is None else str(r["work_id"]),
                work_title=None if r["work_title"] is None else str(r["work_title"]),
                plays_now_file_id=_uuid(r["now_file_id"]),
                plays_now_source=r["now_source"],
            )
            for r in rows
        }

    def files_shown(self, file_ids: Iterable[UUID]) -> dict[UUID, FileShown]:
        rows = self._conn.execute(
            """SELECT id, file_path, track_title, file_status FROM library_files
               WHERE id = ANY(%s)""",
            (sorted(set(file_ids)),),
        ).fetchall()
        return {
            UUID(str(r["id"])): FileShown(
                file_id=UUID(str(r["id"])),
                file_path=str(r["file_path"]),
                track_title=r["track_title"],
                file_status=str(r["file_status"]),
            )
            for r in rows
        }

    def credit_names(self, performance_ids: Iterable[UUID]) -> dict[UUID, list[str]]:
        names: dict[UUID, list[str]] = {}
        for r in self._conn.execute(_CREDIT_NAMES, (sorted(set(performance_ids)),)).fetchall():
            names.setdefault(UUID(str(r["performance_id"])), []).append(str(r["name"]))
        return names

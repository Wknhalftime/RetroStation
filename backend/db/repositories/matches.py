from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.db.repositories._version_entry_states import (
    ON_CONFLICT_COMBINE,
    ON_CONFLICT_KEEP_STATE,
)
from backend.domain.enums import MatchTier, TargetType
from backend.domain.matching import Match, MatchingStorageError, MatchPin
from backend.repositories.matches import MatchRepository

# The version texts of the lines a file plays for, by play_file_resolution's rule (0050): the
# line is auto or manual matched and its best match with a file (highest confidence, then
# created_at, id) is this file. Starts from the file's own matches, so it reads a few rows.
_FILE_LINE_TEXTS = """
    SELECT ti.version_text
    FROM matches fm
    JOIN track_identities ti ON ti.id = fm.identity_id
    WHERE fm.library_file_id = %(file)s
      AND ti.version_text IS NOT NULL
      AND ti.match_status IN ('auto_matched', 'manual_matched')
      AND fm.id = (
          SELECT m.id FROM matches m
          WHERE m.identity_id = ti.id AND m.library_file_id IS NOT NULL
          ORDER BY m.confidence_score DESC, m.created_at, m.id
          LIMIT 1)"""


@translate_lost_connection(MatchingStorageError)
class PgMatchRepository(MatchRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> Match:
        return Match(
            id=row["id"],
            confidence_score=row["confidence_score"],
            match_tier=MatchTier(row["match_tier"]),
            identity_id=row["identity_id"],
            library_file_id=row.get("library_file_id"),
            target_id=row.get("target_id"),
            target_type=TargetType(row["target_type"]) if row.get("target_type") else None,
            trace_id=row.get("trace_id"),
            created_at=row["created_at"],
        )

    def create(self, match: Match) -> Match:
        self._conn.execute(
            """INSERT INTO matches
               (id, confidence_score, match_tier, identity_id,
                library_file_id, target_id, target_type, trace_id, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                match.id,
                match.confidence_score,
                match.match_tier.value,
                match.identity_id,
                match.library_file_id,
                match.target_id,
                match.target_type.value if match.target_type else None,
                match.trace_id,
                match.created_at,
            ),
        )
        row = self._conn.execute("SELECT * FROM matches WHERE id = %s", (match.id,)).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def get_by_identity(self, identity_id: UUID) -> Match | None:
        row = self._conn.execute(
            "SELECT * FROM matches WHERE identity_id = %s", (identity_id,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def copy_version_states(self, file_id: UUID, from_work_id: str, to_work_id: str) -> None:
        self._conn.execute(
            f"""INSERT INTO version_entry_states (work_id, version_text, state, seen_at)
               SELECT %(to)s, st.version_text, st.state, st.seen_at
               FROM version_entry_states st
               WHERE st.work_id = %(from)s AND st.version_text IN ({_FILE_LINE_TEXTS})
               {ON_CONFLICT_COMBINE}""",
            {"to": to_work_id, "from": from_work_id, "file": str(file_id)},
        )

    def see_version_states(self, file_id: UUID, from_work_id: str, to_work_id: str) -> None:
        self._conn.execute(
            f"""INSERT INTO version_entry_states (work_id, version_text, state, seen_at)
               SELECT %(to)s, st.version_text, NULL, st.seen_at
               FROM version_entry_states st
               WHERE st.work_id = %(from)s AND st.version_text IN ({_FILE_LINE_TEXTS})
               {ON_CONFLICT_KEEP_STATE}""",
            {"to": to_work_id, "from": from_work_id, "file": str(file_id)},
        )

    def move_matches_of_works(self, work_ids: list[str], target_id: str) -> None:
        src = ", ".join("%s" for _ in work_ids)
        self._conn.execute(
            f"""
            UPDATE matches
            SET target_id = %s
            WHERE target_type = %s AND target_id IN ({src})
            """,
            [target_id, TargetType.WORK.value, *work_ids],
        )

    def set_stored_performance(self, match_ids: list[UUID], performance_id: UUID | None) -> None:
        if not match_ids:
            return
        self._conn.execute(
            "UPDATE matches SET performance_id = %s WHERE id = ANY(%s)",
            (performance_id, match_ids),
        )

    def set_pins(self, match_ids: list[UUID], file_id: UUID | None) -> None:
        if not match_ids:
            return
        self._conn.execute(
            "UPDATE matches SET pinned_file_id = %s WHERE id = ANY(%s)", (file_id, match_ids)
        )

    def pins_of_lines(self, identity_ids: list[UUID]) -> list[MatchPin]:
        rows = self._conn.execute(
            """SELECT id, identity_id, pinned_file_id FROM matches
               WHERE identity_id = ANY(%s) AND pinned_file_id IS NOT NULL
               ORDER BY identity_id, id""",
            (identity_ids,),
        ).fetchall()
        return [
            MatchPin(
                match_id=UUID(str(r["id"])),
                identity_id=UUID(str(r["identity_id"])),
                file_id=UUID(str(r["pinned_file_id"])),
            )
            for r in rows
        ]

    def release_pins_of_file(self, file_id: UUID) -> int:
        cur = self._conn.execute(
            "UPDATE matches SET pinned_file_id = NULL WHERE pinned_file_id = %s", (file_id,)
        )
        return cur.rowcount

    def delete_for_identity(self, identity_id: UUID) -> None:
        self._conn.execute("DELETE FROM matches WHERE identity_id = %s", (identity_id,))

    def delete_for_file(self, file_id: UUID) -> list[UUID]:
        rows = self._conn.execute(
            "DELETE FROM matches WHERE library_file_id = %s RETURNING identity_id",
            (str(file_id),),
        ).fetchall()
        return [r["identity_id"] for r in rows]

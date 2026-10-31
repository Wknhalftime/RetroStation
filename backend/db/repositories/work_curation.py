"""PostgreSQL adapter of :class:`WorkCurationRepository`: the works router's write statements,
moved here unchanged in phase 0c (C75), on the request's sync connection."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.domain.catalog import Recording, RecordingVersion
from backend.domain.curation import CurationStorageError, FormatOverride, MasterCandidate
from backend.domain.enums import TargetType, VersionType
from backend.repositories.work_curation import WorkCurationRepository


def _placeholders(values: list[str]) -> str:
    return ", ".join("%s" for _ in values)


@translate_lost_connection(CurationStorageError)
class PgWorkCurationRepository(WorkCurationRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    # --- merge ---------------------------------------------------------------------------

    def move_files_of_works(self, work_ids: list[str], target_id: str) -> int:
        cur = self._conn.execute(
            f"UPDATE library_files SET work_id = %s WHERE work_id IN ({_placeholders(work_ids)})",
            [target_id, *work_ids],
        )
        # rowcount can be -1 (unknown) for some driver modes; clamp to 0.
        return max(0, cur.rowcount)

    def drop_colliding_overrides(self, work_ids: list[str], target_id: str) -> int:
        src = _placeholders(work_ids)
        # Drop source overrides that collide with an existing target override
        # (target wins) OR collide with another source override for the same
        # format_name (oldest source wins, id tie-break) so the subsequent bulk
        # UPDATE cannot violate the UNIQUE (work_id, format_name) constraint.
        drop_rows = self._conn.execute(
            f"""
            SELECT id FROM format_overrides
            WHERE work_id IN ({src})
              AND (
                format_name IN (
                  SELECT format_name FROM format_overrides WHERE work_id = %s
                )
                OR id NOT IN (
                  SELECT DISTINCT ON (format_name) id
                  FROM format_overrides
                  WHERE work_id IN ({src})
                    AND format_name NOT IN (
                      SELECT format_name FROM format_overrides WHERE work_id = %s
                    )
                  ORDER BY format_name, created_at ASC, id ASC
                )
              )
            """,
            [*work_ids, target_id, *work_ids, target_id],
        ).fetchall()
        if drop_rows:
            drop_ids = [r["id"] for r in drop_rows]
            self._conn.execute(
                f"DELETE FROM format_overrides WHERE id IN ({_placeholders(drop_ids)})",
                drop_ids,
            )
        return len(drop_rows)

    def move_overrides_of_works(self, work_ids: list[str], target_id: str) -> None:
        src = _placeholders(work_ids)
        self._conn.execute(
            f"UPDATE format_overrides SET work_id = %s WHERE work_id IN ({src})",
            [target_id, *work_ids],
        )

    def recordings_of_works(self, work_ids: list[str]) -> list[RecordingVersion]:
        if not work_ids:
            return []
        rows = self._conn.execute(
            f"SELECT id, version_type FROM recordings WHERE work_id IN ({_placeholders(work_ids)})",
            work_ids,
        ).fetchall()
        return [
            RecordingVersion(id=r["id"], version_type=VersionType(r["version_type"])) for r in rows
        ]

    def move_recording_files(self, from_recording_id: str, to_recording_id: str) -> None:
        self._conn.execute(
            "UPDATE library_files SET recording_id = %s WHERE recording_id = %s",
            (to_recording_id, from_recording_id),
        )

    def delete_recording(self, recording_id: str) -> None:
        self._conn.execute("DELETE FROM recordings WHERE id = %s", (recording_id,))

    def move_recording(self, recording_id: str, work_id: str) -> None:
        self._conn.execute(
            "UPDATE recordings SET work_id = %s WHERE id = %s", (work_id, recording_id)
        )

    def move_matches_of_works(self, work_ids: list[str], target_id: str) -> None:
        src = _placeholders(work_ids)
        self._conn.execute(
            f"""
            UPDATE matches
            SET target_id = %s
            WHERE target_type = %s AND target_id IN ({src})
            """,
            [target_id, TargetType.WORK.value, *work_ids],
        )
        self._conn.execute(
            f"UPDATE matches SET work_id = %s WHERE work_id IN ({src})",
            [target_id, *work_ids],
        )

    def delete_masters_of_works(self, work_ids: list[str]) -> None:
        self._conn.execute(
            f"DELETE FROM song_masters WHERE work_id IN ({_placeholders(work_ids)})",
            work_ids,
        )

    def delete_works(self, work_ids: list[str]) -> int:
        rows = self._conn.execute(
            f"DELETE FROM works WHERE id IN ({_placeholders(work_ids)}) RETURNING id",
            work_ids,
        ).fetchall()
        return len(rows)

    def artist_has_match(self, artist_id: str) -> bool:
        row = self._conn.execute(
            "SELECT id FROM matches WHERE target_type = %s AND target_id = %s LIMIT 1",
            (TargetType.ARTIST.value, artist_id),
        ).fetchone()
        return row is not None

    def delete_artist(self, artist_id: str) -> None:
        self._conn.execute("DELETE FROM artists WHERE id = %s", (artist_id,))

    # --- split and reassign --------------------------------------------------------------

    def insert_recording(self, recording: Recording) -> None:
        self._conn.execute(
            """INSERT INTO recordings
                   (id, title, work_id, version_type, duration_ms,
                    needs_enhancement)
               VALUES (%s, %s, %s, %s, %s, FALSE)""",
            (
                recording.id,
                recording.title,
                recording.work_id,
                recording.version_type.value,
                recording.duration_ms,
            ),
        )

    def set_file_recording(self, file_id: UUID, recording_id: str) -> None:
        self._conn.execute(
            "UPDATE library_files SET recording_id = %s WHERE id = %s",
            (recording_id, str(file_id)),
        )

    def count_other_files_on_recording(self, recording_id: str, file_id: UUID) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM library_files WHERE recording_id = %s AND id != %s",
            (recording_id, str(file_id)),
        ).fetchone()
        return int(row["cnt"]) if row is not None else 0

    def store_split_master(self, master_id: UUID, work_id: str, file_id: UUID) -> None:
        self._conn.execute(
            """
            INSERT INTO song_masters
                (id, work_id, preferred_file_id, selection_method, score, updated_at)
            VALUES (%s, %s, %s, 'auto', NULL, now())
            """,
            (master_id, work_id, file_id),
        )

    # --- a work a file left ----------------------------------------------------------------

    def release_work_matches(self, work_id: str) -> None:
        # matches.work_id is an FK without ON DELETE, so this must run before the work is
        # deleted. A match without a file is left with a NULL work_id.
        self._conn.execute(
            """
            UPDATE matches m
            SET work_id = (
                SELECT lf.work_id FROM library_files lf WHERE lf.id = m.library_file_id
            )
            WHERE m.work_id = %s
            """,
            (work_id,),
        )

    def delete_overrides_of_work(self, work_id: str) -> None:
        self._conn.execute("DELETE FROM format_overrides WHERE work_id = %s", (work_id,))

    def delete_recordings_of_work(self, work_id: str) -> None:
        self._conn.execute("DELETE FROM recordings WHERE work_id = %s", (work_id,))

    # --- the master recalculation ------------------------------------------------------------

    def present_master_candidates(self, work_id: str) -> list[MasterCandidate]:
        rows = self._conn.execute(
            """
            SELECT
                lf.id,
                lf.release_status,
                lf.release_type,
                lf.format,
                lf.bitrate,
                lf.duration_ms
            FROM library_files lf
            WHERE lf.work_id = %s AND lf.file_status = 'present'
            """,
            (work_id,),
        ).fetchall()
        return [
            MasterCandidate(
                id=r["id"],
                release_status=r["release_status"],
                release_type=r["release_type"],
                format=r["format"],
                bitrate=r["bitrate"],
                duration_ms=r["duration_ms"],
            )
            for r in rows
        ]

    def delete_master_off_work(self, work_id: str) -> None:
        self._conn.execute(
            """
            DELETE FROM song_masters sm
            WHERE sm.work_id = %s
              AND NOT EXISTS (
                  SELECT 1 FROM library_files lf
                  WHERE lf.id = sm.preferred_file_id AND lf.work_id = sm.work_id
              )
            """,
            (work_id,),
        )

    def store_auto_master(self, master_id: UUID, work_id: str, file_id: UUID, score: int) -> None:
        self._conn.execute(
            """
            INSERT INTO song_masters
                (id, work_id, preferred_file_id, selection_method, score, updated_at)
            VALUES (%s, %s, %s, 'auto', %s, now())
            ON CONFLICT (work_id) DO UPDATE SET
                preferred_file_id = EXCLUDED.preferred_file_id,
                selection_method  = 'auto',
                score             = EXCLUDED.score,
                updated_at        = now()
            """,
            (master_id, work_id, file_id, score),
        )

    # --- format overrides ------------------------------------------------------------------

    def insert_override_if_absent(self, override: FormatOverride) -> bool:
        row = self._conn.execute(
            """
            INSERT INTO format_overrides (id, work_id, format_name, preferred_file_id, notes)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (work_id, format_name) DO NOTHING
            RETURNING id
            """,
            (
                override.id,
                override.work_id,
                override.format_name,
                override.preferred_file_id,
                override.notes,
            ),
        ).fetchone()
        return row is not None

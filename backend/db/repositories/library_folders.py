from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.domain.library import LibraryFolder
from backend.repositories.library_folders import LibraryFolderRepository


class PgLibraryFolderRepository(LibraryFolderRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> LibraryFolder:
        return LibraryFolder(
            id=row["id"],
            parent_id=row.get("parent_id"),
            name=row["name"],
            full_path=row["full_path"],
            folder_hash=row.get("folder_hash"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def upsert(self, folder: LibraryFolder) -> None:
        self._conn.execute(
            """
            INSERT INTO library_folders (id, parent_id, name, full_path, folder_hash)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (full_path) DO UPDATE SET
                parent_id   = EXCLUDED.parent_id,
                name        = EXCLUDED.name,
                folder_hash = EXCLUDED.folder_hash,
                updated_at  = NOW()
            """,
            (folder.id, folder.parent_id, folder.name, folder.full_path, folder.folder_hash),
        )

    def get_by_path(self, full_path: str) -> LibraryFolder | None:
        row = self._conn.execute(
            "SELECT * FROM library_folders WHERE full_path = %s", (full_path,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_children(self, parent_id: UUID) -> list[LibraryFolder]:
        rows = self._conn.execute(
            "SELECT * FROM library_folders WHERE parent_id = %s", (parent_id,)
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_all(self) -> list[LibraryFolder]:
        rows = self._conn.execute(
            "SELECT * FROM library_folders ORDER BY full_path"
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def update_hash(self, folder_id: UUID, folder_hash: str) -> None:
        self._conn.execute(
            "UPDATE library_folders SET folder_hash = %s, updated_at = NOW() WHERE id = %s",
            (folder_hash, folder_id),
        )

    def stage_hashes(self, hashes: list[tuple[UUID, str]], task_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO library_folder_staged_hashes (folder_id, new_hash, staged_by_task)
                VALUES (%s, %s, %s)
                ON CONFLICT (folder_id, staged_by_task) DO UPDATE SET
                    new_hash  = EXCLUDED.new_hash,
                    staged_at = NOW()
                """,
                [(folder_id, new_hash, task_id) for folder_id, new_hash in hashes],
            )

    def commit_staged_hashes(self, task_id: str) -> int:
        result = self._conn.execute(
            """
            UPDATE library_folders f
            SET folder_hash = s.new_hash, updated_at = NOW()
            FROM library_folder_staged_hashes s
            WHERE f.id = s.folder_id AND s.staged_by_task = %s
            """,
            (task_id,),
        )
        count = result.rowcount
        self.clear_staged_hashes(task_id)
        return count

    def clear_staged_hashes(self, task_id: str) -> None:
        self._conn.execute(
            "DELETE FROM library_folder_staged_hashes WHERE staged_by_task = %s",
            (task_id,),
        )

    def get_folders_with_staged_hashes(self) -> set[UUID]:
        """Return folder IDs that have uncommitted staged hashes."""
        rows = self._conn.execute(
            "SELECT DISTINCT folder_id FROM library_folder_staged_hashes"
        ).fetchall()
        return {row["folder_id"] for row in rows}

    def has_any(self) -> bool:
        row = self._conn.execute(
            "SELECT EXISTS(SELECT 1 FROM library_folders) AS has_rows"
        ).fetchone()
        return bool(row["has_rows"]) if row else False

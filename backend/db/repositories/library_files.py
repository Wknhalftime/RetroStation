from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import psycopg

from backend.domain.enums import EnrichmentStatus, FileStatus, ReleaseStatus, ReleaseType
from backend.domain.models import LibraryFile
from backend.repositories.library_files import LibraryFileRepository


class PgLibraryFileRepository(LibraryFileRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> LibraryFile:
        raw = row.get("raw_metadata")
        if isinstance(raw, str):
            raw = json.loads(raw)

        release_type_val = row.get("release_type")
        release_status_val = row.get("release_status")

        return LibraryFile(
            id=row["id"],
            file_path=row["file_path"],
            file_hash=row["file_hash"],
            format=row.get("format") or "unknown",
            enrichment_status=EnrichmentStatus(row["enrichment_status"]),
            file_status=FileStatus(row.get("file_status", "PRESENT")),
            indexed_at=row["indexed_at"],
            trace_id=row.get("trace_id"),
            recording_id=row.get("recording_id"),
            recording_mbid=row.get("recording_mbid"),
            artist_mbid=row.get("artist_mbid"),
            album_artist_mbid=row.get("album_artist_mbid"),
            release_mbid=row.get("release_mbid"),
            release_title=row.get("release_title"),
            release_type=ReleaseType(release_type_val) if release_type_val else None,
            release_type_secondary=row.get("release_type_secondary"),
            release_status=ReleaseStatus(release_status_val) if release_status_val else None,
            track_title=row.get("track_title"),
            track_number=row.get("track_number"),
            disc_number=row.get("disc_number"),
            duration_ms=row.get("duration_ms"),
            bitrate=row.get("bitrate"),
            raw_metadata=raw,
        )

    def upsert(self, file: LibraryFile) -> LibraryFile:
        self._conn.execute(
            """
            INSERT INTO library_files (
                id, file_path, file_hash, format, enrichment_status,
                trace_id, recording_id, recording_mbid, artist_mbid,
                album_artist_mbid, release_mbid, release_title, release_type,
                release_type_secondary, release_status, track_title,
                track_number, disc_number, duration_ms, bitrate, raw_metadata,
                indexed_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s,
                NOW()
            )
            ON CONFLICT (file_path) DO UPDATE SET
                file_hash              = EXCLUDED.file_hash,
                format                 = EXCLUDED.format,
                enrichment_status      = CASE
                    WHEN library_files.file_hash = EXCLUDED.file_hash
                    THEN library_files.enrichment_status
                    ELSE EXCLUDED.enrichment_status
                END,
                file_status            = 'PRESENT',
                trace_id               = EXCLUDED.trace_id,
                recording_id           = EXCLUDED.recording_id,
                recording_mbid         = EXCLUDED.recording_mbid,
                artist_mbid            = EXCLUDED.artist_mbid,
                album_artist_mbid      = EXCLUDED.album_artist_mbid,
                release_mbid           = EXCLUDED.release_mbid,
                release_title          = EXCLUDED.release_title,
                release_type           = EXCLUDED.release_type,
                release_type_secondary = EXCLUDED.release_type_secondary,
                release_status         = EXCLUDED.release_status,
                track_title            = EXCLUDED.track_title,
                track_number           = EXCLUDED.track_number,
                disc_number            = EXCLUDED.disc_number,
                duration_ms            = EXCLUDED.duration_ms,
                bitrate                = EXCLUDED.bitrate,
                raw_metadata           = EXCLUDED.raw_metadata,
                indexed_at             = NOW()
            """,
            (
                file.id,
                file.file_path,
                file.file_hash,
                file.format,
                file.enrichment_status.value,
                file.trace_id,
                file.recording_id,
                file.recording_mbid,
                file.artist_mbid,
                file.album_artist_mbid,
                file.release_mbid,
                file.release_title,
                file.release_type.value if file.release_type else None,
                file.release_type_secondary,
                file.release_status.value if file.release_status else None,
                file.track_title,
                file.track_number,
                file.disc_number,
                file.duration_ms,
                file.bitrate,
                json.dumps(file.raw_metadata) if file.raw_metadata is not None else None,
            ),
        )
        row = self._conn.execute(
            "SELECT * FROM library_files WHERE file_path = %s",
            (file.file_path,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def upsert_write_only(self, file: LibraryFile) -> None:
        """INSERT or UPDATE a library file without reading back the row."""
        self._conn.execute(
            """
            INSERT INTO library_files (
                id, file_path, file_hash, format, enrichment_status,
                trace_id, recording_id, recording_mbid, artist_mbid,
                album_artist_mbid, release_mbid, release_title, release_type,
                release_type_secondary, release_status, track_title,
                track_number, disc_number, duration_ms, bitrate, raw_metadata,
                indexed_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s,
                NOW()
            )
            ON CONFLICT (file_path) DO UPDATE SET
                file_hash              = EXCLUDED.file_hash,
                format                 = EXCLUDED.format,
                enrichment_status      = CASE
                    WHEN library_files.file_hash = EXCLUDED.file_hash
                    THEN library_files.enrichment_status
                    ELSE EXCLUDED.enrichment_status
                END,
                file_status            = 'PRESENT',
                trace_id               = EXCLUDED.trace_id,
                recording_id           = EXCLUDED.recording_id,
                recording_mbid         = EXCLUDED.recording_mbid,
                artist_mbid            = EXCLUDED.artist_mbid,
                album_artist_mbid      = EXCLUDED.album_artist_mbid,
                release_mbid           = EXCLUDED.release_mbid,
                release_title          = EXCLUDED.release_title,
                release_type           = EXCLUDED.release_type,
                release_type_secondary = EXCLUDED.release_type_secondary,
                release_status         = EXCLUDED.release_status,
                track_title            = EXCLUDED.track_title,
                track_number           = EXCLUDED.track_number,
                disc_number            = EXCLUDED.disc_number,
                duration_ms            = EXCLUDED.duration_ms,
                bitrate                = EXCLUDED.bitrate,
                raw_metadata           = EXCLUDED.raw_metadata,
                indexed_at             = NOW()
            """,
            (
                file.id,
                file.file_path,
                file.file_hash,
                file.format,
                file.enrichment_status.value,
                file.trace_id,
                file.recording_id,
                file.recording_mbid,
                file.artist_mbid,
                file.album_artist_mbid,
                file.release_mbid,
                file.release_title,
                file.release_type.value if file.release_type else None,
                file.release_type_secondary,
                file.release_status.value if file.release_status else None,
                file.track_title,
                file.track_number,
                file.disc_number,
                file.duration_ms,
                file.bitrate,
                json.dumps(file.raw_metadata) if file.raw_metadata is not None else None,
            ),
        )

    def get_by_id(self, id: UUID) -> LibraryFile | None:
        row = self._conn.execute(
            "SELECT * FROM library_files WHERE id = %s", (id,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_by_path(self, file_path: str) -> LibraryFile | None:
        row = self._conn.execute(
            "SELECT * FROM library_files WHERE file_path = %s", (file_path,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_by_recording(self, recording_id: str) -> list[LibraryFile]:
        rows = self._conn.execute(
            "SELECT * FROM library_files WHERE recording_id = %s",
            (recording_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_by_artist_mbid(self, artist_mbid: str) -> list[LibraryFile]:
        rows = self._conn.execute(
            """SELECT * FROM library_files
               WHERE artist_mbid = %s OR album_artist_mbid = %s""",
            (artist_mbid, artist_mbid),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_pending_enrichment_by_release(self, release_mbid: str) -> list[LibraryFile]:
        rows = self._conn.execute(
            """SELECT * FROM library_files
               WHERE release_mbid = %s
                 AND enrichment_status = %s""",
            (release_mbid, EnrichmentStatus.PENDING.value),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_pending_enrichment_by_recording(self, recording_mbid: str) -> list[LibraryFile]:
        rows = self._conn.execute(
            """SELECT * FROM library_files
               WHERE recording_mbid = %s
                 AND enrichment_status = %s
                 AND release_mbid IS NULL""",
            (recording_mbid, EnrichmentStatus.PENDING.value),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def update_recording_link(
        self,
        id: UUID,
        recording_id: str | None,
        enrichment_status: EnrichmentStatus,
    ) -> None:
        self._conn.execute(
            """UPDATE library_files
               SET recording_id = %s, enrichment_status = %s
               WHERE id = %s""",
            (recording_id, enrichment_status.value, id),
        )

    def count_by_format(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT format, COUNT(*) AS cnt FROM library_files GROUP BY format"
        ).fetchall()
        return {r["format"]: r["cnt"] for r in rows}

    def count_by_enrichment_status(self) -> dict[str, int]:
        rows = self._conn.execute(
            """SELECT enrichment_status, COUNT(*) AS cnt
               FROM library_files GROUP BY enrichment_status"""
        ).fetchall()
        return {r["enrichment_status"]: r["cnt"] for r in rows}

    def get_by_folder_path(self, folder_path: str) -> list[LibraryFile]:
        """Return all files directly in folder_path (not in subfolders).

        Handles both ``/`` and ``\\`` separators so queries work on Windows
        (where stored paths use backslashes) and POSIX alike.
        """
        stripped = folder_path.rstrip("/").rstrip("\\")
        sep = "\\" if "\\" in stripped else "/"
        prefix = stripped + sep
        rows = self._conn.execute(
            """SELECT * FROM library_files
               WHERE file_path LIKE %s
                 AND file_path NOT LIKE %s""",
            (prefix + "%", prefix + "%" + sep + "%"),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def mark_missing(self, file_path: str) -> None:
        self._conn.execute(
            "UPDATE library_files SET file_status = 'MISSING' WHERE file_path = %s",
            (file_path,),
        )

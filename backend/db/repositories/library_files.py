from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import psycopg

from backend.domain.enums import EnrichmentStatus, FileStatus, ReleaseStatus, ReleaseType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.repositories.library_file_enrichment import LibraryFileEnrichmentRepository
from backend.repositories.library_files import LibraryFileRepository


class PgLibraryFileRepository(LibraryFileRepository, LibraryFileEnrichmentRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> LibraryFile:
        raw = row.get("raw_metadata")
        if isinstance(raw, str):
            raw = json.loads(raw)

        release_type_val = row.get("release_type")
        release_status_val = row.get("release_status")

        audio = AudioMetadata(
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
            artist_name=row.get("artist_name"),
            normalized_artist_name=row.get("normalized_artist_name"),
            normalized_title=row.get("normalized_title"),
        )

        return LibraryFile(
            id=row["id"],
            file_path=row["file_path"],
            file_hash=row["file_hash"],
            format=row.get("format") or "unknown",
            enrichment_status=EnrichmentStatus(row["enrichment_status"]),
            file_status=FileStatus(row.get("file_status", "present")),
            indexed_at=row["indexed_at"],
            trace_id=row.get("trace_id"),
            recording_id=row.get("recording_id"),
            work_id=row.get("work_id"),
            audio=audio,
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
                artist_name, normalized_artist_name, normalized_title,
                work_id,
                indexed_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
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
                file_status            = 'present',
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
                work_id                = CASE
                    WHEN library_files.file_hash = EXCLUDED.file_hash
                    THEN library_files.work_id
                    ELSE NULL
                END,
                normalized_artist_name = COALESCE(
                    NULLIF(TRIM(EXCLUDED.normalized_artist_name), ''),
                    library_files.normalized_artist_name),
                normalized_title       = COALESCE(
                    NULLIF(TRIM(EXCLUDED.normalized_title), ''),
                    library_files.normalized_title),
                artist_name            = COALESCE(
                    NULLIF(TRIM(EXCLUDED.artist_name), ''),
                    library_files.artist_name),
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
                file.audio.recording_mbid,
                file.audio.artist_mbid,
                file.audio.album_artist_mbid,
                file.audio.release_mbid,
                file.audio.release_title,
                file.audio.release_type.value if file.audio.release_type else None,
                file.audio.release_type_secondary,
                file.audio.release_status.value if file.audio.release_status else None,
                file.audio.track_title,
                file.audio.track_number,
                file.audio.disc_number,
                file.audio.duration_ms,
                file.audio.bitrate,
                json.dumps(file.audio.raw_metadata)
                if file.audio.raw_metadata is not None else None,
                file.audio.artist_name,
                file.audio.normalized_artist_name,
                file.audio.normalized_title,
                file.work_id,
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
                artist_name, normalized_artist_name, normalized_title,
                work_id,
                indexed_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
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
                file_status            = 'present',
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
                work_id                = CASE
                    WHEN library_files.file_hash = EXCLUDED.file_hash
                    THEN library_files.work_id
                    ELSE NULL
                END,
                normalized_artist_name = COALESCE(
                    NULLIF(TRIM(EXCLUDED.normalized_artist_name), ''),
                    library_files.normalized_artist_name),
                normalized_title       = COALESCE(
                    NULLIF(TRIM(EXCLUDED.normalized_title), ''),
                    library_files.normalized_title),
                artist_name            = COALESCE(
                    NULLIF(TRIM(EXCLUDED.artist_name), ''),
                    library_files.artist_name),
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
                file.audio.recording_mbid,
                file.audio.artist_mbid,
                file.audio.album_artist_mbid,
                file.audio.release_mbid,
                file.audio.release_title,
                file.audio.release_type.value if file.audio.release_type else None,
                file.audio.release_type_secondary,
                file.audio.release_status.value if file.audio.release_status else None,
                file.audio.track_title,
                file.audio.track_number,
                file.audio.disc_number,
                file.audio.duration_ms,
                file.audio.bitrate,
                json.dumps(file.audio.raw_metadata)
                if file.audio.raw_metadata is not None else None,
                file.audio.artist_name,
                file.audio.normalized_artist_name,
                file.audio.normalized_title,
                file.work_id,
            ),
        )

    def get_by_id(self, file_id: UUID) -> LibraryFile | None:
        row = self._conn.execute(
            "SELECT * FROM library_files WHERE id = %s", (file_id,)
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

    def search_by_artist_name(
        self, normalized_name: str, limit: int = 100,
    ) -> list[LibraryFile]:
        needle = f"%{normalized_name.lower().strip()}%"
        rows = self._conn.execute(
            """SELECT * FROM library_files
               WHERE LOWER(TRIM(artist_name)) LIKE %s
               LIMIT %s""",
            (needle, limit),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_by_recording_mbid(self, recording_mbid: str) -> LibraryFile | None:
        row = self._conn.execute(
            """SELECT * FROM library_files
               WHERE recording_mbid = %s
               LIMIT 1""",
            (recording_mbid,),
        ).fetchone()
        return self._row_to_model(row) if row else None

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
        file_id: UUID,
        recording_id: str | None,
        enrichment_status: EnrichmentStatus,
    ) -> None:
        self._conn.execute(
            """UPDATE library_files
               SET recording_id = %s, enrichment_status = %s
               WHERE id = %s""",
            (recording_id, enrichment_status.value, file_id),
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
            "UPDATE library_files SET file_status = %s WHERE file_path = %s",
            (FileStatus.MISSING, file_path),
        )

    def update_work_id(
        self, file_id: UUID, work_id: str | None,
    ) -> None:
        self._conn.execute(
            "UPDATE library_files SET work_id = %s WHERE id = %s",
            (work_id, str(file_id)),
        )

    def get_by_hash(self, file_hash: str) -> list[LibraryFile]:
        rows = self._conn.execute(
            "SELECT * FROM library_files WHERE file_hash = %s",
            (file_hash,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def reset_failed_enrichments(self) -> int:
        """Reset all files in 'failed' enrichment status back to 'pending'.

        Returns the number of rows updated.
        """
        result = self._conn.execute(
            """UPDATE library_files
               SET enrichment_status = %s
               WHERE enrichment_status = %s""",
            (EnrichmentStatus.PENDING.value, EnrichmentStatus.FAILED.value),
        )
        return result.rowcount if result.rowcount is not None else 0

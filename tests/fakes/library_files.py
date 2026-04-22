import dataclasses
from pathlib import Path
from uuid import UUID

from backend.domain.enums import EnrichmentStatus, FileStatus
from backend.domain.library import LibraryFile
from backend.repositories.library_file_enrichment import LibraryFileEnrichmentRepository
from backend.repositories.library_files import LibraryFileRepository


class FakeLibraryFileRepository(LibraryFileRepository, LibraryFileEnrichmentRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, LibraryFile] = {}

    def upsert(self, file: LibraryFile) -> LibraryFile:
        existing = self.get_by_path(file.file_path)
        if existing:
            if existing.file_hash == file.file_hash:
                file.enrichment_status = existing.enrichment_status
            file.file_status = FileStatus.PRESENT
            self._data[existing.id] = file
            return file
        self._data[file.id] = file
        return file

    def upsert_write_only(self, file: LibraryFile) -> None:
        self.upsert(file)

    def get_by_id(self, file_id: UUID) -> LibraryFile | None:
        return self._data.get(file_id)

    def get_by_path(self, file_path: str) -> LibraryFile | None:
        return next((f for f in self._data.values() if f.file_path == file_path), None)

    def get_by_recording(self, recording_id: str) -> list[LibraryFile]:
        return [f for f in self._data.values() if f.recording_id == recording_id]

    def get_by_artist_mbid(self, artist_mbid: str) -> list[LibraryFile]:
        return [f for f in self._data.values() if f.audio.artist_mbid == artist_mbid]

    def search_by_artist_name(
        self, normalized_name: str, limit: int = 100,
    ) -> list[LibraryFile]:
        needle = normalized_name.strip().lower()
        hits = [
            f for f in self._data.values()
            if needle in (f.audio.artist_name or "").lower()
        ]
        return hits[:limit]

    def get_by_recording_mbid(self, recording_mbid: str) -> LibraryFile | None:
        for f in self._data.values():
            if f.audio.recording_mbid == recording_mbid:
                return f
        return None

    def get_pending_enrichment_by_release(self, release_mbid: str) -> list[LibraryFile]:
        return [
            f for f in self._data.values()
            if f.audio.release_mbid == release_mbid
            and f.enrichment_status == EnrichmentStatus.PENDING
        ]

    def get_pending_enrichment_by_recording(self, recording_mbid: str) -> list[LibraryFile]:
        return [
            f for f in self._data.values()
            if f.audio.recording_mbid == recording_mbid
            and f.audio.release_mbid is None
            and f.enrichment_status == EnrichmentStatus.PENDING
        ]

    def update_recording_link(
        self, file_id: UUID, recording_id: str | None, enrichment_status: EnrichmentStatus
    ) -> None:
        if f := self._data.get(file_id):
            f.recording_id = recording_id
            f.enrichment_status = enrichment_status

    def count_by_format(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self._data.values():
            counts[f.format] = counts.get(f.format, 0) + 1
        return counts

    def count_by_enrichment_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self._data.values():
            key = f.enrichment_status.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    def get_by_folder_path(self, folder_path: str) -> list[LibraryFile]:
        # Normalise to a Path so the separator check works on both POSIX and Windows.
        folder = Path(folder_path)
        result = []
        for f in self._data.values():
            try:
                rel = Path(f.file_path).relative_to(folder)
            except ValueError:
                continue
            # Only direct children (no sub-directory components)
            if len(rel.parts) == 1:
                result.append(f)
        return result

    def mark_missing(self, file_path: str) -> None:
        for f in self._data.values():
            if f.file_path == file_path:
                f.file_status = FileStatus.MISSING
                break

    def update_work_id(self, file_id: UUID, work_id: str | None) -> None:
        if file_id in self._data:
            self._data[file_id] = dataclasses.replace(
                self._data[file_id], work_id=work_id,
            )

    def get_by_hash(self, file_hash: str) -> list[LibraryFile]:
        return [f for f in self._data.values() if f.file_hash == file_hash]

    def reset_failed_enrichments(self) -> int:
        count = 0
        for f in self._data.values():
            if f.enrichment_status == EnrichmentStatus.FAILED:
                f.enrichment_status = EnrichmentStatus.PENDING
                count += 1
        return count

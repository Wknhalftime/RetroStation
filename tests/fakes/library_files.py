from uuid import UUID

from backend.domain.enums import EnrichmentStatus
from backend.domain.models import LibraryFile
from backend.repositories.library_files import LibraryFileRepository


class FakeLibraryFileRepository(LibraryFileRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, LibraryFile] = {}

    def upsert(self, file: LibraryFile) -> LibraryFile:
        existing = self.get_by_path(file.file_path)
        if existing:
            self._data[existing.id] = file
            return file
        self._data[file.id] = file
        return file

    def get_by_id(self, id: UUID) -> LibraryFile | None:
        return self._data.get(id)

    def get_by_path(self, file_path: str) -> LibraryFile | None:
        return next((f for f in self._data.values() if f.file_path == file_path), None)

    def get_by_recording(self, recording_id: str) -> list[LibraryFile]:
        return [f for f in self._data.values() if f.recording_id == recording_id]

    def get_by_artist_mbid(self, artist_mbid: str) -> list[LibraryFile]:
        return [f for f in self._data.values() if f.artist_mbid == artist_mbid]

    def get_pending_enrichment_by_release(self, release_mbid: str) -> list[LibraryFile]:
        return [
            f for f in self._data.values()
            if f.release_mbid == release_mbid
            and f.enrichment_status == EnrichmentStatus.PENDING
        ]

    def get_pending_enrichment_by_recording(self, recording_mbid: str) -> list[LibraryFile]:
        return [
            f for f in self._data.values()
            if f.recording_mbid == recording_mbid
            and f.release_mbid is None
            and f.enrichment_status == EnrichmentStatus.PENDING
        ]

    def update_recording_link(
        self, id: UUID, recording_id: str | None, enrichment_status: EnrichmentStatus
    ) -> None:
        if f := self._data.get(id):
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

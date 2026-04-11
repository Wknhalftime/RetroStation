from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.enums import EnrichmentStatus
from backend.domain.library import LibraryFile


class LibraryFileRepository(ABC):
    @abstractmethod
    def upsert(self, file: LibraryFile) -> LibraryFile: ...

    @abstractmethod
    def upsert_write_only(self, file: LibraryFile) -> None: ...

    @abstractmethod
    def get_by_id(self, id: UUID) -> LibraryFile | None: ...

    @abstractmethod
    def get_by_path(self, file_path: str) -> LibraryFile | None: ...

    @abstractmethod
    def get_by_recording(self, recording_id: str) -> list[LibraryFile]: ...

    @abstractmethod
    def get_by_artist_mbid(self, artist_mbid: str) -> list[LibraryFile]: ...

    @abstractmethod
    def get_pending_enrichment_by_release(self, release_mbid: str) -> list[LibraryFile]: ...

    @abstractmethod
    def get_pending_enrichment_by_recording(self, recording_mbid: str) -> list[LibraryFile]: ...

    @abstractmethod
    def update_recording_link(
        self,
        id: UUID,
        recording_id: str | None,
        enrichment_status: EnrichmentStatus,
    ) -> None: ...

    @abstractmethod
    def count_by_format(self) -> dict[str, int]: ...

    @abstractmethod
    def count_by_enrichment_status(self) -> dict[str, int]: ...

    @abstractmethod
    def get_by_folder_path(self, folder_path: str) -> list[LibraryFile]: ...

    @abstractmethod
    def mark_missing(self, file_path: str) -> None: ...

    @abstractmethod
    def update_work_id(self, file_id: UUID, work_id: str | None) -> None: ...

    @abstractmethod
    def get_by_hash(self, file_hash: str) -> list[LibraryFile]:
        """Return all files with the given content hash."""
        ...

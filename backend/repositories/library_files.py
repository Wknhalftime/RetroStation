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
    def get_by_id(self, file_id: UUID) -> LibraryFile | None: ...

    @abstractmethod
    def get_by_path(self, file_path: str) -> LibraryFile | None: ...

    @abstractmethod
    def get_by_recording(self, recording_id: str) -> list[LibraryFile]: ...

    @abstractmethod
    def get_by_artist_mbid(self, artist_mbid: str) -> list[LibraryFile]: ...

    @abstractmethod
    def search_by_artist_name(
        self, normalized_name: str, limit: int = 100
    ) -> list[LibraryFile]:
        """Return library files whose artist name matches normalized_name by
        substring (case-insensitive). Used by BroadcastToLocalStrategy when the
        artist MBID is not yet confirmed. Pre-filter step — the calling strategy
        scores with rapidfuzz.
        """
        ...

    @abstractmethod
    def get_by_recording_mbid(self, recording_mbid: str) -> list[LibraryFile]:
        """Return all library files whose recording_mbid matches.
        Used by ResolvedArtistMbidStrategy Step B after MB recording search.
        Multiple rows are possible (e.g., different encodes of the same
        recording); caller scores and picks the best.
        """
        ...

    @abstractmethod
    def update_recording_link(
        self,
        file_id: UUID,
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


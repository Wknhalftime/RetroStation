from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.enums import EnrichmentStatus, FileStatus
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
    def get_by_normalized_artist_name(
        self, normalized_name: str, limit: int = 100
    ) -> list[LibraryFile]:
        """Return library files whose stored ``normalized_artist_name`` is
        EXACTLY equal to the given normalized name. Callers pass the broadcast
        artist's ``normalized_name`` directly — both sides are produced by
        ``backend.services.normalization.normalize_artist`` so equality is
        well-defined. Substring matching was deliberately retired to enforce
        the no-cross-artist invariant in the Resolution Center; recall trade-off
        (tag spelling drift) is intentional.
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
    def get_path_statuses_under(self, root: str) -> dict[str, FileStatus]:
        """Path -> status of every file anywhere beneath *root* (recursive)."""
        ...

    @abstractmethod
    def mark_missing(self, file_path: str) -> None: ...

    @abstractmethod
    def update_work_id(self, file_id: UUID, work_id: str | None) -> None: ...

    @abstractmethod
    def get_by_work(self, work_id: str) -> list[LibraryFile]: ...

    @abstractmethod
    def relocate(self, file_id: UUID, new_path: str) -> None:
        """Point an existing row at the path its file was moved or renamed to.

        The row keeps its id and every link; only ``file_path`` changes and
        the file is PRESENT again.
        """
        ...

    @abstractmethod
    def get_by_hash(self, file_hash: str) -> list[LibraryFile]:
        """Return all files with the given content hash."""
        ...

    @abstractmethod
    def update_file_stat(self, file_id: UUID, file_size: int, file_mtime_ns: int) -> None:
        """Record the on-disk size and mtime without touching any other column.

        Used to backfill rows indexed before stat tracking existed, once a
        scan has established the file is unchanged.
        """
        ...

    @abstractmethod
    def get_unhashed_by_stat(self, file_size: int, file_mtime_ns: int) -> list[LibraryFile]:
        """Rows with no content hash yet whose recorded size and mtime equal these.

        Move detection's fallback while a first scan's hashes are being
        filled in: a move or rename on one volume keeps size and mtime.
        """
        ...


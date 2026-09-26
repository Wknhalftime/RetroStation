from abc import ABC, abstractmethod

from backend.domain.library import LibraryFile


class LibraryFileEnrichmentRepository(ABC):
    @abstractmethod
    def get_pending_enrichment_by_release(self, release_mbid: str) -> list[LibraryFile]: ...

    @abstractmethod
    def get_pending_enrichment_by_recording(self, recording_mbid: str) -> list[LibraryFile]: ...

    @abstractmethod
    def get_pending_enrichment_with_release(self) -> list[LibraryFile]:
        """Every pending file that has both a release and a recording MBID.

        These are the files one batched recording search can resolve;
        ordered by file path so a run is reproducible.
        """
        ...

    @abstractmethod
    def reset_failed_enrichments(self) -> int:
        """Reset all files in 'failed' enrichment status back to 'pending'.

        Returns the number of rows updated.
        """
        ...

from abc import ABC, abstractmethod

from backend.domain.library import LibraryFile


class LibraryFileEnrichmentRepository(ABC):
    @abstractmethod
    def get_pending_enrichment_by_release(self, release_mbid: str) -> list[LibraryFile]: ...

    @abstractmethod
    def get_pending_enrichment_by_recording(self, recording_mbid: str) -> list[LibraryFile]: ...

    @abstractmethod
    def reset_failed_enrichments(self) -> int:
        """Reset all files in 'failed' enrichment status back to 'pending'.

        Returns the number of rows updated.
        """
        ...

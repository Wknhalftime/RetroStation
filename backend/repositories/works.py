from abc import ABC, abstractmethod

from backend.domain.models import Work


class WorkRepository(ABC):
    @abstractmethod
    def upsert(self, work: Work) -> Work: ...

    @abstractmethod
    def get_by_id(self, mbid: str) -> Work | None: ...

    @abstractmethod
    def get_by_artist(self, artist_id: str) -> list[Work]: ...

    @abstractmethod
    def list_needing_enhancement(self) -> list[Work]: ...

    @abstractmethod
    def mark_enhanced(self, mbid: str) -> None: ...

    @abstractmethod
    def update_embedding(self, mbid: str, embedding: list[float]) -> None: ...

    @abstractmethod
    def create_local(self, title: str, artist_id: str) -> str:
        """Create a local-origin work. Returns work id."""
        ...

    @abstractmethod
    def upsert_from_mb(self, mbid: str, title: str, artist_id: str) -> str:
        """Collision check with FOR UPDATE. Merge or create. Returns work id."""
        ...

    @abstractmethod
    def get_by_mbid(self, mbid: str) -> Work | None: ...

    @abstractmethod
    def delete_if_empty(self, work_id: str) -> bool:
        """Delete work if no library_files reference it. Returns True if deleted."""
        ...

    @abstractmethod
    def get_candidates_by_normalized_artist(
        self, normalized_artist_name: str, limit: int = 100,
    ) -> list[tuple[str, str]]:
        """Return (work_id, work_title) pairs for fuzzy matching.

        Deduplicates by work_id so each work appears at most once.
        Ordered by work title for stable results.
        """
        ...

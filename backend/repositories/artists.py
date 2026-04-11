from abc import ABC, abstractmethod

from backend.domain.catalog import Artist


class ArtistRepository(ABC):
    @abstractmethod
    def upsert(self, artist: Artist) -> Artist: ...

    @abstractmethod
    def get_by_id(self, mbid: str) -> Artist | None: ...

    @abstractmethod
    def list_all(self) -> list[Artist]:
        """Return all artists for fuzzy-matching in artist_matching_service."""
        ...

    @abstractmethod
    def list_needing_enhancement(self) -> list[Artist]: ...

    @abstractmethod
    def mark_enhanced(self, mbid: str) -> None: ...

    @abstractmethod
    def mark_enhancement_failed(self, mbid: str, error: str) -> None: ...

    @abstractmethod
    def upsert_local(self, name: str, normalized_name: str) -> str:
        """Create local artist or return existing by normalized_name.
        INSERT ON CONFLICT (normalized_name) DO NOTHING + retry-SELECT.
        Returns artist id.
        """
        ...

    @abstractmethod
    def upsert_from_mb(
        self,
        mbid: str,
        name: str,
        sort_name: str,
        disambiguation: str | None = None,
    ) -> str:
        """Lookup by mbid or normalized_name, promote/create/reuse.
        Returns artist id (may be a promoted local UUID).
        """
        ...

    @abstractmethod
    def get_by_normalized_name(self, normalized_name: str) -> Artist | None: ...

from abc import ABC, abstractmethod

from backend.domain.models import Artist


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

from abc import ABC, abstractmethod

from backend.domain.catalog import Artist


class ArtistEnhancementRepository(ABC):
    @abstractmethod
    def list_unenhanced(self) -> list[Artist]: ...

    @abstractmethod
    def mark_enhanced(self, artist_id: str) -> None: ...

    @abstractmethod
    def mark_enhancement_failed(self, artist_id: str, error: str) -> None: ...

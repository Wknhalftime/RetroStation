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

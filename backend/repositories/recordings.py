from abc import ABC, abstractmethod

from backend.domain.models import Recording


class RecordingRepository(ABC):
    @abstractmethod
    def upsert(self, recording: Recording) -> Recording: ...

    @abstractmethod
    def get_by_id(self, mbid: str) -> Recording | None: ...

    @abstractmethod
    def get_by_work(self, work_id: str) -> list[Recording]: ...

    @abstractmethod
    def update_embedding(self, mbid: str, embedding: list[float]) -> None: ...

    @abstractmethod
    def list_needing_enhancement(self) -> list[Recording]: ...

    @abstractmethod
    def mark_enhanced(self, mbid: str) -> None: ...

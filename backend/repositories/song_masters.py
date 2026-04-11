from abc import ABC, abstractmethod

from backend.domain.curation import SongMaster


class SongMasterRepository(ABC):
    @abstractmethod
    def upsert(self, master: SongMaster) -> SongMaster: ...

    @abstractmethod
    def get_by_work(self, work_id: str) -> SongMaster | None: ...

    @abstractmethod
    def list_auto_for_works(self, work_ids: list[str]) -> list[SongMaster]:
        """Return auto-selected masters for the given work IDs (skip manual selections)."""
        ...

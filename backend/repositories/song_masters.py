from abc import ABC, abstractmethod

from backend.domain.curation import SongMaster


class SongMasterRepository(ABC):
    @abstractmethod
    def upsert(self, master: SongMaster) -> SongMaster:
        """Store the work's pick unless the stored one is manual; return the stored row."""
        ...

    @abstractmethod
    def replace(self, master: SongMaster) -> None:
        """Store the work's pick whatever the stored one's method.

        Only for a manual pick that is no longer valid (its file is missing or left the
        work); ``upsert`` keeps refusing to overwrite a manual pick.
        """
        ...

    @abstractmethod
    def delete_by_work(self, work_id: str) -> None:
        """Remove the work's song master, if it has one."""
        ...

    @abstractmethod
    def get_by_work(self, work_id: str) -> SongMaster | None: ...

    @abstractmethod
    def list_auto_for_works(self, work_ids: list[str]) -> list[SongMaster]:
        """Return auto-selected masters for the given work IDs (skip manual selections)."""
        ...

    @abstractmethod
    def list_work_ids_with_missing_master(self) -> list[str]:
        """Works whose AUTO master's file is missing but that have a present file, by id.

        Manual masters are left for the user to change, even on a missing file.
        """
        ...

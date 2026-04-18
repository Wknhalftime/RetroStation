from abc import ABC, abstractmethod
from uuid import UUID


class LibraryFolderHashStaging(ABC):
    @abstractmethod
    def stage_hashes(self, hashes: list[tuple[UUID, str]], task_id: str) -> None: ...

    @abstractmethod
    def commit_staged_hashes(self, task_id: str) -> int: ...

    @abstractmethod
    def clear_staged_hashes(self, task_id: str) -> None: ...

    @abstractmethod
    def get_folders_with_staged_hashes(self) -> set[UUID]: ...

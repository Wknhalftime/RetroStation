from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.models import LibraryFolder


class LibraryFolderRepository(ABC):
    @abstractmethod
    def upsert(self, folder: LibraryFolder) -> None: ...

    @abstractmethod
    def get_by_path(self, full_path: str) -> LibraryFolder | None: ...

    @abstractmethod
    def get_children(self, parent_id: UUID) -> list[LibraryFolder]: ...

    @abstractmethod
    def get_all(self) -> list[LibraryFolder]: ...

    @abstractmethod
    def update_hash(self, folder_id: UUID, folder_hash: str) -> None: ...

    @abstractmethod
    def stage_hashes(self, hashes: list[tuple[UUID, str]], task_id: str) -> None: ...

    @abstractmethod
    def commit_staged_hashes(self, task_id: str) -> int: ...

    @abstractmethod
    def clear_staged_hashes(self, task_id: str) -> None: ...

    @abstractmethod
    def get_folders_with_staged_hashes(self) -> set[UUID]: ...

    @abstractmethod
    def has_any(self) -> bool: ...

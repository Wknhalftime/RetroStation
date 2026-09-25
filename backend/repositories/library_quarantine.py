from abc import ABC, abstractmethod

from backend.domain.library import LibraryQuarantine


class LibraryQuarantineRepository(ABC):
    @abstractmethod
    def create(self, entry: LibraryQuarantine) -> LibraryQuarantine: ...

    @abstractmethod
    def create_write_only(self, entry: LibraryQuarantine) -> None: ...

    @abstractmethod
    def list_all(self) -> list[LibraryQuarantine]: ...

    @abstractmethod
    def get_by_path(self, file_path: str) -> LibraryQuarantine | None: ...

    @abstractmethod
    def get_paths_under(self, root: str) -> set[str]:
        """Paths of every entry anywhere beneath *root* (recursive)."""
        ...

    @abstractmethod
    def delete_by_path(self, file_path: str) -> None:
        """Remove every entry for *file_path*, including historical duplicates."""
        ...

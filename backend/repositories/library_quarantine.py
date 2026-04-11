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

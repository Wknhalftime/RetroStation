from pathlib import Path

from backend.domain.library import LibraryQuarantine
from backend.repositories.library_quarantine import LibraryQuarantineRepository


class FakeLibraryQuarantineRepository(LibraryQuarantineRepository):
    def __init__(self) -> None:
        self._data: list[LibraryQuarantine] = []

    def create(self, entry: LibraryQuarantine) -> LibraryQuarantine:
        self._data.append(entry)
        return entry

    def create_write_only(self, entry: LibraryQuarantine) -> None:
        self.create(entry)

    def list_all(self) -> list[LibraryQuarantine]:
        return list(self._data)

    def get_by_path(self, file_path: str) -> LibraryQuarantine | None:
        return next((e for e in self._data if e.file_path == file_path), None)

    def get_paths_under(self, root: str) -> set[str]:
        base = Path(root)
        return {e.file_path for e in self._data if base in Path(e.file_path).parents}

    def delete_by_path(self, file_path: str) -> None:
        self._data = [e for e in self._data if e.file_path != file_path]

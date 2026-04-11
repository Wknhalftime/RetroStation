from __future__ import annotations

from uuid import UUID

from backend.domain.library import LibraryFolder
from backend.repositories.library_folders import LibraryFolderRepository


class FakeLibraryFolderRepository(LibraryFolderRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, LibraryFolder] = {}
        self._staged: dict[str, list[tuple[UUID, str]]] = {}

    def upsert(self, folder: LibraryFolder) -> None:
        existing = self.get_by_path(folder.full_path)
        if existing:
            self._data[existing.id] = folder
        else:
            self._data[folder.id] = folder

    def get_by_path(self, full_path: str) -> LibraryFolder | None:
        return next((f for f in self._data.values() if f.full_path == full_path), None)

    def get_children(self, parent_id: UUID) -> list[LibraryFolder]:
        return [f for f in self._data.values() if f.parent_id == parent_id]

    def get_all(self) -> list[LibraryFolder]:
        return list(self._data.values())

    def update_hash(self, folder_id: UUID, folder_hash: str) -> None:
        if folder_id in self._data:
            self._data[folder_id].folder_hash = folder_hash

    def stage_hashes(self, hashes: list[tuple[UUID, str]], task_id: str) -> None:
        self._staged[task_id] = hashes

    def commit_staged_hashes(self, task_id: str) -> int:
        staged = self._staged.pop(task_id, [])
        for folder_id, new_hash in staged:
            self.update_hash(folder_id, new_hash)
        return len(staged)

    def clear_staged_hashes(self, task_id: str) -> None:
        self._staged.pop(task_id, None)

    def get_folders_with_staged_hashes(self) -> set[UUID]:
        """Return folder IDs that have uncommitted staged hashes."""
        result: set[UUID] = set()
        for staged_list in self._staged.values():
            for folder_id, _ in staged_list:
                result.add(folder_id)
        return result

    def has_any(self) -> bool:
        return len(self._data) > 0

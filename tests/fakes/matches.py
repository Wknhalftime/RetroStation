from dataclasses import replace
from uuid import UUID

from backend.domain.matching import Match, MatchOffFileWork
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.matches import MatchRepository


class FakeMatchRepository(MatchRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, Match] = {}
        # Files' works are the library file repo's; tests wire one in via
        # set_library_file_repo for the off-work listing.
        self._library_file_repo: LibraryFileRepository | None = None

    def set_library_file_repo(self, repo: LibraryFileRepository) -> None:
        """Inject the library file repo the off-work listing reads files' works from."""
        self._library_file_repo = repo

    def list_off_file_work(self) -> list[MatchOffFileWork]:
        # Mirrors PgMatchRepository; without a library file repo nothing is known.
        files = self._library_file_repo
        if files is None:
            return []
        found = set()
        for match in self._data.values():
            file = files.get_by_id(match.library_file_id) if match.library_file_id else None
            if file is not None and match.work_id != file.work_id:
                found.add(MatchOffFileWork(file.id, match.work_id, file.work_id))
        return sorted(found, key=lambda d: (str(d.library_file_id), d.match_work_id or ""))

    def create(self, match: Match) -> Match:
        self._data[match.id] = match
        return match

    def get_by_identity(self, identity_id: UUID) -> Match | None:
        return next((m for m in self._data.values() if m.identity_id == identity_id), None)

    def get_by_artist(self, artist_id: UUID) -> Match | None:
        return next((m for m in self._data.values() if m.artist_id == artist_id), None)

    def move_to_work(self, file_id: UUID, work_id: str | None) -> None:
        for match_id, match in list(self._data.items()):
            if match.library_file_id == file_id:
                self._data[match_id] = replace(match, work_id=work_id)

    def delete_for_identity(self, identity_id: UUID) -> None:
        to_delete = [id for id, m in self._data.items() if m.identity_id == identity_id]
        for id in to_delete:
            del self._data[id]

    def delete_for_file(self, file_id: UUID) -> list[UUID]:
        doomed = [m for m in self._data.values() if m.library_file_id == file_id]
        for m in doomed:
            del self._data[m.id]
        return [m.identity_id for m in doomed if m.identity_id is not None]

from dataclasses import replace
from uuid import UUID

from backend.domain.enums import TargetType
from backend.domain.matching import Match
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.matches import MatchRepository


class FakeMatchRepository(MatchRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, Match] = {}
        # set_library_file_repo for release_work_matches, which reads each match's file.
        self._library_file_repo: LibraryFileRepository | None = None

    def set_library_file_repo(self, repo: LibraryFileRepository) -> None:
        """Wire in the files a released match takes its work from (PostgreSQL reads them)."""
        self._library_file_repo = repo

    def create(self, match: Match) -> Match:
        self._data[match.id] = match
        return match

    def get_by_identity(self, identity_id: UUID) -> Match | None:
        return next((m for m in self._data.values() if m.identity_id == identity_id), None)

    def copy_version_states(self, file_id: UUID, from_work_id: str, to_work_id: str) -> None:
        # Version-entry states live in PostgreSQL only; the integration tests check them.
        return None

    def see_version_states(self, file_id: UUID, from_work_id: str, to_work_id: str) -> None:
        # Version-entry states live in PostgreSQL only; the integration tests check them.
        return None

    def move_to_work(self, file_id: UUID, work_id: str | None) -> None:
        for match_id, match in list(self._data.items()):
            if match.library_file_id == file_id:
                self._data[match_id] = replace(match, work_id=work_id)

    def move_matches_of_works(self, work_ids: list[str], target_id: str) -> None:
        for match in list(self._data.values()):
            if match.target_type == TargetType.WORK and match.target_id in work_ids:
                self._data[match.id] = match = replace(match, target_id=target_id)
            if match.work_id in work_ids:
                self._data[match.id] = replace(match, work_id=target_id)

    def release_work_matches(self, work_id: str) -> None:
        files = self._library_file_repo
        for match in [m for m in self._data.values() if m.work_id == work_id]:
            file = (
                files.get_by_id(match.library_file_id)
                if files is not None and match.library_file_id is not None
                else None
            )
            self._data[match.id] = replace(match, work_id=file.work_id if file else None)

    def delete_for_identity(self, identity_id: UUID) -> None:
        to_delete = [id for id, m in self._data.items() if m.identity_id == identity_id]
        for id in to_delete:
            del self._data[id]

    def delete_for_file(self, file_id: UUID) -> list[UUID]:
        doomed = [m for m in self._data.values() if m.library_file_id == file_id]
        for m in doomed:
            del self._data[m.id]
        return [m.identity_id for m in doomed]

from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.matching import Match


class MatchRepository(ABC):
    @abstractmethod
    def create(self, match: Match) -> Match: ...

    @abstractmethod
    def get_by_identity(self, identity_id: UUID) -> Match | None: ...

    @abstractmethod
    def get_by_artist(self, artist_id: UUID) -> Match | None: ...

    @abstractmethod
    def delete_for_identity(self, identity_id: UUID) -> None: ...

    @abstractmethod
    def move_to_work(self, file_id: UUID, work_id: str | None) -> None:
        """Point every match on the file at the work the file now belongs to.

        ``matches.work_id`` mirrors ``library_files.work_id`` for the matched
        file, so it follows the file whenever the file changes work.
        """
        ...

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

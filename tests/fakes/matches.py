from uuid import UUID
from backend.domain.models import Match
from backend.repositories.matches import MatchRepository


class FakeMatchRepository(MatchRepository):
    def __init__(self) -> None:
        self._data: dict[UUID, Match] = {}

    def create(self, match: Match) -> Match:
        self._data[match.id] = match
        return match

    def get_by_identity(self, identity_id: UUID) -> Match | None:
        return next((m for m in self._data.values() if m.identity_id == identity_id), None)

    def get_by_artist(self, artist_id: UUID) -> Match | None:
        return next((m for m in self._data.values() if m.artist_id == artist_id), None)

    def delete_for_identity(self, identity_id: UUID) -> None:
        to_delete = [id for id, m in self._data.items() if m.identity_id == identity_id]
        for id in to_delete:
            del self._data[id]

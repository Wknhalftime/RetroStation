from collections.abc import Sequence
from uuid import UUID

from backend.domain.curation import PlayFileResolution
from backend.repositories.play_file_resolution import PlayFileResolutionRepository


class FakePlayFileResolutionRepository(PlayFileResolutionRepository):
    """A lookup table of resolutions: tests seed what the view would answer per play.

    It holds no resolution rules of its own; those belong to the view alone (D17).
    """

    def __init__(self) -> None:
        self._data: dict[UUID, PlayFileResolution] = {}

    def add(self, resolution: PlayFileResolution) -> None:
        """Test helper: record what the view answers for one play."""
        self._data[resolution.play_event_id] = resolution

    def get_for_plays(self, play_event_ids: Sequence[UUID]) -> dict[UUID, PlayFileResolution]:
        return {
            event_id: self._data.get(event_id, PlayFileResolution(event_id, None, None))
            for event_id in play_event_ids
        }

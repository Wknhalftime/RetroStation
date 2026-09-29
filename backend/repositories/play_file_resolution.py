from abc import ABC, abstractmethod
from collections.abc import Sequence
from uuid import UUID

from backend.domain.curation import PlayFileResolution


class PlayFileResolutionRepository(ABC):
    """Reads curation's view ``play_file_resolution`` (spec D17): which file plays per play."""

    @abstractmethod
    def get_for_plays(self, play_event_ids: Sequence[UUID]) -> dict[UUID, PlayFileResolution]:
        """The resolution of each play, keyed by play event id.

        Every requested id is a key; a play that resolves to no file (or that the view has no
        row for) maps to a resolution with ``file_id`` and ``file_status`` ``None``.
        """
        ...

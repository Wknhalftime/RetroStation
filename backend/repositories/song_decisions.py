from abc import ABC, abstractmethod
from collections.abc import Sequence
from uuid import UUID


class SongDecisionRepository(ABC):
    """A song's row, as the curator's Resolve, Reject and Unmatch read and write it (phase 3a
    PR 1, C75). Its matches are written through ``SongMatchService``."""

    @abstractmethod
    def exists(self, identity_id: UUID) -> bool: ...

    @abstractmethod
    def lock_status(self, identity_id: UUID) -> str | None:
        """Lock the song's row (``FOR UPDATE``) and read its status; None for no such song."""
        ...

    @abstractmethod
    def matched_file_ids(self, identity_id: UUID) -> list[UUID]:
        """The files the song's matches name."""
        ...

    @abstractmethod
    def set_matched_by_hand(self, identity_id: UUID) -> None:
        """The song is manual_matched with the manual tier; its reason stays."""
        ...

    @abstractmethod
    def park_rejected(self, identity_id: UUID, file_ids: Sequence[UUID]) -> None:
        """Add *file_ids* to the song's rejected files and park it as manual_rejected /
        USER_UNMATCHED, with no tier and no reason detail (AUD-R029)."""
        ...

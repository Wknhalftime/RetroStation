"""The whole-name Reject and Unmatch cascade's reads and writes on the name's lines (phase 1b,
M24). What changes is decided by the domain's ``plan_cascade``, never here; a name's links are
written through ``LogNameLinkService``.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from uuid import UUID

from backend.domain.artist_cascade import CascadeLine


class ArtistCascadeRepository(ABC):
    @abstractmethod
    def lines_of(self, broadcast_artist_id: UUID, *, lock: bool = False) -> list[CascadeLine]:
        """Every line of the log name, with its status, its plays and whether the match it
        plays (a match to a file, the highest score first, then the oldest) is to a file whose
        ``normalized_artist_name`` is the name's ``normalized_name``. Empty for an unknown name.

        ``lock`` holds the lines until the transaction ends, so a matcher's write on one waits
        for the action that read them.
        """
        ...

    @abstractmethod
    def reject_lines(self, identity_ids: Sequence[UUID]) -> None:
        """Set the lines auto_rejected, and nothing else of them."""
        ...

    @abstractmethod
    def reopen_lines(self, identity_ids: Sequence[UUID]) -> None:
        """Put the lines back in review (USER_UNMATCHED), tier and detail cleared."""
        ...

    @abstractmethod
    def delete_matches_of_lines(self, identity_ids: Sequence[UUID]) -> None:
        """Delete every match row of the lines."""
        ...

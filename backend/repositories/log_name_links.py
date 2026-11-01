"""Port for log-name links, ``log_name_links`` (C30, AUD-R035).

``LogNameLinkService`` (``services/log_name_link_service.py``) is its only caller, so the rules
for links live there and this port stays complete as it is: later phases add rules without
changing it or its fake.
"""

from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.enums import LogNameLinkState
from backend.domain.matching import LogNameLink


class LogNameLinkRepository(ABC):
    @abstractmethod
    def list_by_name(self, broadcast_artist_id: UUID) -> list[LogNameLink]:
        """Every link of the log name, in every state, oldest first (``created_at``, then id)."""
        ...

    @abstractmethod
    def list_by_artist(self, artist_id: str) -> list[LogNameLink]:
        """Every link to the catalog artist, in every state, oldest first."""
        ...

    @abstractmethod
    def upsert_link(self, link: LogNameLink) -> LogNameLink:
        """Store the link and return the stored row.

        One row per (name, artist): when the pair has one, it takes the link's MBID, state,
        origin, strength, evidence and credit columns, and keeps its id and ``created_at``.

        Raises:
            LogNameLinkTargetMissingError: the log name or the artist does not exist.
        """
        ...

    @abstractmethod
    def set_link_state(
        self, broadcast_artist_id: UUID, artist_id: str, state: LogNameLinkState
    ) -> bool:
        """Set the pair's state; whether the pair has a row."""
        ...

    @abstractmethod
    def delete_links_of_name(self, broadcast_artist_id: UUID) -> int:
        """Delete every link of the log name, in every state; how many."""
        ...

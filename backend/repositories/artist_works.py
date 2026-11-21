from __future__ import annotations

from abc import ABC, abstractmethod

from backend.domain.artist_page import GroupWork, ListedWorks


class ArtistWorksReader(ABC):
    """The artist pages' works reads, by credits through merge roots (3b PR 5b, S7, R6, R9)."""

    @abstractmethod
    async def listed_works(self, root_ids: list[str]) -> dict[str, ListedWorks]:
        """Each listed artist's works and file count; an artist with none is left out."""

    @abstractmethod
    async def group_works(self, member_ids: list[str]) -> list[GroupWork]:
        """The works any of *member_ids* is credited on, by title."""

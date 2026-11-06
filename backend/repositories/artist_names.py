"""Every name of an artist (phase 2a S10, C43). A log spelling has no source, so it can't get in."""

from abc import ABC, abstractmethod

from backend.domain.catalog import ArtistName
from backend.domain.enums import ArtistNameSource


class ArtistNameRepository(ABC):
    @abstractmethod
    def add(
        self, artist_id: str, name: str, normalized_name: str, source: ArtistNameSource
    ) -> bool:
        """Add one name; whether it was new. One row per artist, spelling and source."""
        ...

    @abstractmethod
    def list_for_artist(self, artist_id: str) -> list[ArtistName]:
        """The artist's names, by source then spelling."""
        ...

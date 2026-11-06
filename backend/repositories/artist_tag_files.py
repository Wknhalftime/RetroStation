"""Port for the artist identity sweep's one read of ``library_files`` (phase 2a S14, D6)."""

from abc import ABC, abstractmethod

from backend.domain.library import ArtistTagGroup


class ArtistTagFilesRepository(ABC):
    @abstractmethod
    def list_tag_groups(self) -> list[ArtistTagGroup]:
        """The present files of each name whose artist MBID tags hold two or more values (compared
        lowercase), grouped by name, tag value, spelling and title. Values are as stored: telling
        a real MBID from a joined or placeholder one is the caller's. A blank name is skipped."""
        ...

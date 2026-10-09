"""Port for the local-artist linker (AUD-R026; spec 2026-10-05 D15)."""

from abc import ABC, abstractmethod
from datetime import datetime

from backend.domain.catalog import Artist, ArtistLinkCandidate, LinkEvidence
from backend.domain.enums import ArtistLinkOutcome


class ArtistLinkingRepository(ABC):
    """The linker's work list, its library evidence, its two writes, and the re-check's names."""

    @abstractmethod
    def list_due(self) -> list[Artist]:
        """Local artists without an MBID that were never looked up, or that have a present file
        indexed after their last lookup. Ordered by normalized name, then id."""
        ...

    @abstractmethod
    def evidence(self, normalized_name: str) -> LinkEvidence:
        """The distinct artist-MBID tag values of the present files with this normalized artist
        name, each with its present-file count, most files first."""
        ...

    @abstractmethod
    def link(self, artist_id: str, candidate: ArtistLinkCandidate) -> bool:
        """Give the artist the candidate's MBID, name, sort name and disambiguation, in place.

        The origin becomes musicbrainz, and the artist is marked enhanced and looked up
        (linked). Returns False, and writes nothing, when the artist already has an MBID or
        another artist holds this one.
        """
        ...

    @abstractmethod
    def record_outcome(self, artist_id: str, outcome: ArtistLinkOutcome) -> None:
        """Stamp a non-linked outcome and the lookup time on an artist without an MBID.

        Raises:
            InvalidLinkDecisionError: for LINKED, which only link() writes.
        """
        ...

    @abstractmethod
    def normalized_names_linked_since(self, when: datetime) -> set[str]:
        """The targeted re-check's second source of names (AUD-R026): for every artist linked
        after ``when``, its own normalized name and the normalized names of the broadcast
        artists whose artist match targets it."""
        ...

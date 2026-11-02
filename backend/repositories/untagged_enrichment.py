from abc import ABC, abstractmethod

from backend.domain.library import UntaggedCandidate


class UntaggedEnrichmentRepository(ABC):
    """The files the untagged-file enrichment pass can search for."""

    @abstractmethod
    def list_untagged_with_linked_artist(self) -> list[UntaggedCandidate]:
        """Every PRESENT file with no recording and no release MBID in its tags, pending or
        skipped, whose work's artist has a MusicBrainz MBID (its own, or the one the local
        artist linker gave it), with that MBID; ordered by file path so a run is reproducible.

        A file whose artist has no MBID cannot be searched by artist and is not listed.
        """
        ...

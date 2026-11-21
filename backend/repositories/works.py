from abc import ABC, abstractmethod
from collections.abc import Sequence

from backend.domain.catalog import EmptyWork, Work
from backend.domain.performances import WorkArtist


class WorkRepository(ABC):
    @abstractmethod
    def upsert(self, work: Work) -> Work: ...

    @abstractmethod
    def get_by_id(self, mbid: str) -> Work | None: ...

    @abstractmethod
    def works_crediting(self, artist_id: str) -> list[Work]:
        """The works some performance of which credits the artist, by id (3b S7, R1)."""
        ...

    @abstractmethod
    def credited_artist(self, work_id: str, normalized_name: str) -> WorkArtist | None:
        """The artist of *normalized_name* the work's performances credit, the earliest-placed
        one first (the artist grouping picks on a matched work; 3b R1)."""
        ...

    @abstractmethod
    def list_needing_enhancement(self) -> list[Work]: ...

    @abstractmethod
    def mark_enhanced(self, mbid: str) -> None: ...

    @abstractmethod
    def update_embedding(self, mbid: str, embedding: list[float]) -> None: ...

    @abstractmethod
    def create_local(self, title: str) -> str:
        """Create a local-origin work (its artists are its performances' credits, 3b S7).
        Returns work id."""
        ...

    @abstractmethod
    def upsert_from_mb(self, mbid: str, title: str) -> str:
        """Collision check with FOR UPDATE. Merge or create. Returns work id."""
        ...

    @abstractmethod
    def list_musicbrainz_ids(self, work_ids: Sequence[str]) -> set[str]:
        """Of ``work_ids``, those that are MusicBrainz works (``origin = 'musicbrainz'``)."""
        ...

    @abstractmethod
    def get_id_by_mbid(self, mbid: str) -> str | None:
        """The id of the work that holds ``mbid``, or None."""
        ...

    @abstractmethod
    def claim_mbid(self, work_id: str, mbid: str, title: str) -> bool:
        """Make a local work the MusicBrainz work ``mbid`` names (AUD-R103, R1).

        It takes the MBID, origin ``musicbrainz`` and MusicBrainz's ``title``, and is queued
        for the works phase. Refuses, returning False and raising nothing, a work that is not
        local, already has an MBID or does not exist, and an MBID another work holds.
        """
        ...

    @abstractmethod
    def drop_fileless_grouping_recordings(self, work_id: str) -> int:
        """Delete the work's recordings that grouping made and no file points at; how many.

        Grouping's recordings are the ones never enhanced (``needs_enhancement`` FALSE and no
        ``enhanced_at``), the predicate of the recordings phase's orphan delete.
        """
        ...

    @abstractmethod
    def delete_if_empty(self, work_id: str) -> bool:
        """Delete the work unless files, recordings, matches, overrides of either kind or a
        performance a match stores reference it (C53, 3b S6).

        Its song master goes with it. Returns True if deleted.
        """
        ...

    @abstractmethod
    def list_empty_work_ids(self) -> list[str]:
        """Every work with no files (the sweep's candidates, 3b S6)."""
        ...

    @abstractmethod
    def kept_empty_works(self) -> list[EmptyWork]:
        """Every work with no files that something points at, with what, by title (D15)."""
        ...

    @abstractmethod
    def get_candidates_by_normalized_artist(
        self,
        normalized_artist_name: str,
        limit: int = 100,
    ) -> list[tuple[str, str]]:
        """Return (work_id, work_title) pairs for fuzzy matching.

        Deduplicates by work_id so each work appears at most once.
        Ordered by work title for stable results.
        """
        ...

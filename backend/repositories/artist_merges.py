"""The port for ``artist_merges``: Merge and Unmerge's edges (phase 2a S11, M33).

The adapter is the only writer of the table (``tests/test_artist_merge_writers.py``), and the
merge service the only caller of its writes. A caller takes ``lock_merges`` first, so two merges
never pass their checks at once.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from backend.domain.catalog import ArtistGroup, ArtistMerge, MergeArtist


class ArtistMergeRepository(ABC):
    @abstractmethod
    def lock_merges(self) -> None:
        """Until the caller's transaction ends, no other Merge or Unmerge runs; reads go on."""

    @abstractmethod
    def artist(self, artist_id: str) -> MergeArtist | None:
        """The artist a Merge checks, or None when there is none."""

    @abstractmethod
    def primary_of(self, artist_id: str) -> str | None:
        """The primary *artist_id* is merged into directly, or None."""

    @abstractmethod
    def artist_group(self, artist_id: str) -> ArtistGroup | None:
        """The artist's group, walked to its root; None for an unknown artist."""

    @abstractmethod
    def is_merged(self, artist_id: str) -> bool:
        """Whether the artist is on either side of an edge."""

    @abstractmethod
    def add_merge(self, merge: ArtistMerge) -> None:
        """Store the edge; an artist already merged keeps its edge as it is. An artist that no
        longer exists is a ``MergeArtistNotFoundError``."""

    @abstractmethod
    def remove_merge(self, artist_id: str) -> None:
        """Delete the artist's own edge, if it has one."""

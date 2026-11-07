"""In-memory :class:`ArtistMergeRepository` (phase 2a S11, M33).

The artists it knows are given up front; edges are kept as made, one per merged artist, and the
group walks each chain to its root, as ``ARTIST_GROUP_SQL`` does. ``lock_merges`` has nothing
to serialize in memory. ``tests/repositories/test_artist_merges_contract.py`` runs the same cases
on this fake and on PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Iterable

from backend.domain.catalog import (
    ArtistGroup,
    ArtistMerge,
    MergeArtist,
    MergeArtistNotFoundError,
)
from backend.repositories.artist_merges import ArtistMergeRepository


class FakeArtistMergeRepository(ArtistMergeRepository):
    def __init__(self, artists: Iterable[MergeArtist] = ()) -> None:
        self.artists = {artist.id: artist for artist in artists}
        self.edges: dict[str, ArtistMerge] = {}

    def lock_merges(self) -> None:
        return None

    def artist(self, artist_id: str) -> MergeArtist | None:
        return self.artists.get(artist_id)

    def primary_of(self, artist_id: str) -> str | None:
        edge = self.edges.get(artist_id)
        return None if edge is None else edge.primary_id

    def _root(self, artist_id: str) -> str | None:
        """The root a chain ends at; None inside a cycle, which never reaches one."""
        seen = {artist_id}
        current = artist_id
        while (primary := self.primary_of(current)) is not None:
            if primary in seen:
                return None
            seen.add(primary)
            current = primary
        return current

    def artist_group(self, artist_id: str) -> ArtistGroup | None:
        if artist_id not in self.artists:
            return None
        root = self._root(artist_id) or artist_id
        members = {root} | {a for a in self.edges if a != root and self._root(a) == root}
        mbids = {mbid for a in members if (mbid := self.artists[a].mbid) is not None}
        return ArtistGroup(
            primary_id=root, member_ids=frozenset(members), member_mbids=frozenset(mbids)
        )

    def is_merged(self, artist_id: str) -> bool:
        return artist_id in self.edges or any(
            edge.primary_id == artist_id for edge in self.edges.values()
        )

    def add_merge(self, merge: ArtistMerge) -> None:
        if merge.artist_id not in self.artists or merge.primary_id not in self.artists:
            raise MergeArtistNotFoundError("An artist of the merge no longer exists")
        self.edges.setdefault(merge.artist_id, merge)

    def remove_merge(self, artist_id: str) -> None:
        self.edges.pop(artist_id, None)

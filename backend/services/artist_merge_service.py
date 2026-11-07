"""Merge and Unmerge on the artist page (phase 2a S11, E4, E5; M33; AUD-R041).

Only the curator merges: a route calls these, in its request's transaction (C75), and nothing
else does (``tests/test_artist_merge_writers.py``). A merge stores one direct edge, as made, and
the group's primary is the root its chain of edges ends at. Song matching doesn't read the group
until 4.3, so neither action queues a re-check.
"""

from __future__ import annotations

from backend.domain.catalog import (
    SPECIAL_PURPOSE_MBIDS,
    ArtistMerge,
    MergeArtist,
    MergeArtistNotFoundError,
    MergeRefusedError,
)
from backend.repositories.artist_merges import ArtistMergeRepository


def _artist(artist_id: str, merges: ArtistMergeRepository) -> MergeArtist:
    found = merges.artist(artist_id)
    if found is None:
        raise MergeArtistNotFoundError(f"Artist {artist_id} not found")
    return found


def _refusal(
    artist: MergeArtist,
    primary: MergeArtist,
    current: str | None,
    merges: ArtistMergeRepository,
) -> str | None:
    """Why merging *artist* (merged into *current* now) into *primary* is refused (E5)."""
    if artist.id == primary.id:
        return "An artist can't be merged into itself"
    if {artist.mbid, primary.mbid} & SPECIAL_PURPOSE_MBIDS:
        return "A special-purpose artist, such as Various Artists, can't be merged"
    if current is not None:
        return f"This artist is already merged into {current}; unmerge it first"
    group = merges.artist_group(primary.id)
    if group is not None and group.primary_id == artist.id:
        return "Merging these would make a cycle"
    return None


def merge_artist(merge: ArtistMerge, merges: ArtistMergeRepository) -> None:
    """Merge ``merge.artist_id`` into ``merge.primary_id``. A repeat changes nothing, its first
    reason kept; a merge into an artist that is itself merged is kept as made."""
    artist = _artist(merge.artist_id, merges)
    primary = _artist(merge.primary_id, merges)
    current = merges.primary_of(artist.id)
    if current == primary.id:
        return
    refusal = _refusal(artist, primary, current, merges)
    if refusal is not None:
        raise MergeRefusedError(refusal)
    merges.add_merge(merge)


def unmerge_artist(artist_id: str, merges: ArtistMergeRepository) -> None:
    """Delete the artist's own edge; an artist with none is left as it is (a repeat)."""
    _artist(artist_id, merges)
    merges.remove_merge(artist_id)

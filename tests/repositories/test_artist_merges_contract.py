"""Merge edges: the fake against PostgreSQL (phase 2a S11, E4; M33).

One edge per merged artist, kept as made; the group walks each chain to its root, with every
member and every member MBID; ``is_merged`` holds on either side of an edge. A merge of an artist
that no longer exists is refused, not left as a foreign-key error. The adapter commits nothing.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.db.repositories.artist_merges import PgArtistMergeRepository
from backend.domain.catalog import ArtistGroup, ArtistMerge, MergeArtist, MergeArtistNotFoundError
from backend.repositories.artist_merges import ArtistMergeRepository
from tests.fakes.artist_merges import FakeArtistMergeRepository

pytestmark = pytest.mark.integration

BAND = "4d7928cd-7ed2-4282-8c29-c0c9f966f1bd"
SINGER = "ee58c59f-8e7f-4430-b8ca-236c4d3745ae"
ARTISTS = (
    MergeArtist(id="artist-a", name="Alice Cooper", mbid=None),
    MergeArtist(id="artist-b", name="Alice Cooper", mbid=SINGER),
    MergeArtist(id="artist-c", name="Alice Cooper", mbid=BAND),
    MergeArtist(id="artist-d", name="Prince", mbid=None),
)


def _seed(conn: psycopg.Connection[DictRow]) -> None:
    for artist in ARTISTS:
        conn.execute(
            """INSERT INTO artists
                   (id, name, sort_name, normalized_name, mbid, origin, needs_enhancement)
               VALUES (%s, %s, %s, %s, %s, %s, FALSE)""",
            (
                artist.id,
                artist.name,
                artist.name,
                None if artist.mbid else f"{artist.name.lower()} {artist.id}",
                artist.mbid,
                "musicbrainz" if artist.mbid else "local",
            ),
        )


@pytest.fixture(params=["fake", "pg"])
def repo(request: pytest.FixtureRequest) -> Iterator[ArtistMergeRepository]:
    if request.param == "fake":
        yield FakeArtistMergeRepository(ARTISTS)
        return
    migrated_db = request.getfixturevalue("migrated_db")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        _seed(conn)
        yield PgArtistMergeRepository(conn)
        conn.rollback()


def _group(primary: str, members: set[str], mbids: set[str]) -> ArtistGroup:
    return ArtistGroup(
        primary_id=primary, member_ids=frozenset(members), member_mbids=frozenset(mbids)
    )


def test_an_artist_is_read_with_its_name_and_mbid(repo: ArtistMergeRepository) -> None:
    repo.lock_merges()

    assert repo.artist("artist-b") == ARTISTS[1]
    assert repo.artist("no-such-artist") is None


def test_an_unmerged_artist_is_its_own_group(repo: ArtistMergeRepository) -> None:
    assert repo.artist_group("artist-c") == _group("artist-c", {"artist-c"}, {BAND})
    assert repo.artist_group("artist-a") == _group("artist-a", {"artist-a"}, set())
    assert repo.artist_group("no-such-artist") is None
    assert repo.primary_of("artist-a") is None
    assert repo.is_merged("artist-a") is False


def test_a_chain_and_a_tree_walk_to_the_root(repo: ArtistMergeRepository) -> None:
    repo.add_merge(ArtistMerge("artist-a", "artist-b", "same act"))
    repo.add_merge(ArtistMerge("artist-b", "artist-c"))
    repo.add_merge(ArtistMerge("artist-d", "artist-c"))

    whole = _group("artist-c", {"artist-a", "artist-b", "artist-c", "artist-d"}, {SINGER, BAND})
    assert [repo.artist_group(a.id) for a in ARTISTS] == [whole] * 4
    assert [repo.primary_of(a.id) for a in ARTISTS] == ["artist-b", "artist-c", None, "artist-c"]
    assert all(repo.is_merged(a.id) for a in ARTISTS)


def test_an_edge_stays_as_first_made(repo: ArtistMergeRepository) -> None:
    repo.add_merge(ArtistMerge("artist-a", "artist-b"))
    repo.add_merge(ArtistMerge("artist-a", "artist-c"))

    assert repo.primary_of("artist-a") == "artist-b"


def test_remove_deletes_only_the_artists_own_edge(repo: ArtistMergeRepository) -> None:
    repo.add_merge(ArtistMerge("artist-a", "artist-b"))
    repo.add_merge(ArtistMerge("artist-b", "artist-c"))

    repo.remove_merge("artist-b")
    repo.remove_merge("artist-d")  # none: nothing happens

    assert repo.primary_of("artist-a") == "artist-b"
    assert repo.artist_group("artist-a") == _group("artist-b", {"artist-a", "artist-b"}, {SINGER})
    assert repo.artist_group("artist-c") == _group("artist-c", {"artist-c"}, {BAND})


def test_a_merge_with_an_artist_gone_is_refused(repo: ArtistMergeRepository) -> None:
    with pytest.raises(MergeArtistNotFoundError):
        repo.add_merge(ArtistMerge("artist-a", "no-such-artist"))

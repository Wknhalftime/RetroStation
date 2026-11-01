"""An unnamed stub artist is not a matching candidate (artist model phase 1a, SPEC S15; S3).

``ArtistCatalogRepository.list_all()`` feeds ``NormalizationStrategy`` its candidates
(``artist_matching_service.build_local_artist_engine``). It leaves out every artist whose name
still equals its MBID (an unnamed stub), on PostgreSQL and in the fake alike; local artists
(no MBID) and named MusicBrainz artists stay. So a log name spelled like a stub's MBID is not
matched to it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.artists import PgArtistRepository
from backend.domain.broadcast import BroadcastArtist
from backend.domain.enums import MatchStatus
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.services.artist_matching_service import (
    ArtistMatchingRepos,
    match_artists_for_playlist,
)
from backend.services.normalization import normalize_artist
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.log_name_links import FakeLogNameLinkRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient

METALLICA = "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"
UNNAMED = "00000000-0000-4000-8000-0000000000aa"
NAMED_STUB = "00000000-0000-4000-8000-0000000000bb"


def _three_artists(catalog: ArtistCatalogRepository) -> tuple[str, str, str, str]:
    """A local artist, a named MusicBrainz artist, an unnamed stub, a stub since named."""
    local = catalog.upsert_local_artist("Van Halen", "van halen")
    named = catalog.upsert_musicbrainz_artist(
        mbid=METALLICA, name="Metallica", sort_name="Metallica", normalized_name="metallica"
    )
    unnamed = catalog.create_stub(UNNAMED)
    was_stub = catalog.create_stub(NAMED_STUB)
    return local, named, unnamed.id, was_stub.id


def _assert_unnamed_stubs_left_out(
    catalog: ArtistCatalogRepository, name_stub: Callable[[str], None]
) -> None:
    local, named, unnamed, was_stub = _three_artists(catalog)
    name_stub(was_stub)

    listed = {artist.id for artist in catalog.list_all()}

    assert listed == {local, named, was_stub}
    assert unnamed not in listed
    stub = catalog.get_by_id(unnamed)
    assert stub is not None and (stub.name, stub.mbid) == (UNNAMED, UNNAMED)


def test_the_fake_leaves_unnamed_stubs_out() -> None:
    catalog = FakeArtistRepository()

    def name_stub(artist_id: str) -> None:
        artist = catalog.get_by_id(artist_id)
        assert artist is not None
        artist.name = artist.sort_name = "Ozzy Osbourne"

    _assert_unnamed_stubs_left_out(catalog, name_stub)


@pytest.fixture
def pg_conn(migrated_db: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        yield conn


@pytest.mark.integration
def test_postgres_leaves_unnamed_stubs_out(pg_conn: psycopg.Connection[Any]) -> None:
    def name_stub(artist_id: str) -> None:
        pg_conn.execute(
            "UPDATE artists SET name = 'Ozzy Osbourne', sort_name = 'Osbourne, Ozzy' WHERE id = %s",
            (artist_id,),
        )

    _assert_unnamed_stubs_left_out(PgArtistRepository(pg_conn), name_stub)


def test_a_log_name_spelled_like_a_stubs_mbid_is_not_matched_to_it() -> None:
    names = FakeBroadcastArtistRepository()
    catalog = FakeArtistRepository()
    stub = catalog.create_stub("mbid-ruleband")
    playlist_id = uuid4()
    spelled = names.upsert(
        BroadcastArtist(
            id=uuid4(), original_name="MBID RULEBAND", normalized_name=normalize_artist(stub.name)
        )
    )
    names.register_playlist_artist(playlist_id, spelled.id)
    links = FakeLogNameLinkRepository(artists=catalog, names=names)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        repos=ArtistMatchingRepos(
            broadcast_artist_repo=names,
            track_identity_repo=FakeBroadcastTrackIdentityRepository(),
            artist_repo=catalog,
            match_repo=FakeMatchRepository(),
            rules_repo=FakeMappingRuleRepository(),
            link_repo=links,
        ),
        mb_client=FakeMbClient(),
    )

    stored = names.get_by_id(spelled.id)
    assert stored is not None and stored.match_status != MatchStatus.AUTO_MATCHED
    assert links.list_by_name(spelled.id) == []

"""``ArtistCatalogRepository.artist_ids_by_mbid``, fake against PostgreSQL (phase 2a S10, S14).

One read of which MBIDs an artist holds, so the sweep needn't ask per MBID after every scan, and the
backfill reaches a stub ``list_all`` hides.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.artists import PgArtistRepository
from backend.repositories.artist_catalog import ArtistCatalogRepository
from tests.fakes.artists import FakeArtistRepository

ONE = "0a000000-0000-4000-8000-000000000001"
TWO = "0a000000-0000-4000-8000-000000000002"
THREE = "0a000000-0000-4000-8000-000000000003"


@pytest.fixture(params=["fake", pytest.param("postgres", marks=pytest.mark.integration)])
def catalog(request: pytest.FixtureRequest) -> Iterator[ArtistCatalogRepository]:
    if request.param == "fake":
        yield FakeArtistRepository()
        return
    migrated_db = cast(str, request.getfixturevalue("migrated_db"))
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        yield PgArtistRepository(conn)


def test_each_held_mbid_gives_its_artist(catalog: ArtistCatalogRepository) -> None:
    named = catalog.upsert_musicbrainz_artist(ONE, "A", "A", "a")

    assert catalog.artist_ids_by_mbid([ONE, TWO]) == {ONE: named}


def test_an_unnamed_stub_is_held(catalog: ArtistCatalogRepository) -> None:
    """``list_all`` hides it, and this mustn't: its MBID has an artist."""
    stub = catalog.create_stub(THREE)

    assert catalog.artist_ids_by_mbid([THREE]) == {THREE: stub.id}


def test_nothing_asked_is_nothing_held(catalog: ArtistCatalogRepository) -> None:
    catalog.upsert_musicbrainz_artist(ONE, "A", "A", "a")

    assert catalog.artist_ids_by_mbid([]) == {}


def test_a_name_only_artist_holds_no_mbid(catalog: ArtistCatalogRepository) -> None:
    catalog.upsert_local_artist("B", "b")

    assert catalog.artist_ids_by_mbid([TWO]) == {}

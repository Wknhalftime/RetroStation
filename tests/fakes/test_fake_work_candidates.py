"""The works fake's candidate lookup mirrors ``PgWorkRepository``'s join on the name (phase 2a S5):
it lists the works of every artist of the name, with an MBID or not. Not locked."""

from __future__ import annotations

from backend.domain.catalog import Artist
from backend.domain.enums import CatalogSource
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.works import FakeWorkRepository


def _artist(artist_id: str, mbid: str | None) -> Artist:
    return Artist(
        id=artist_id,
        name="Alice Cooper",
        sort_name="Alice Cooper",
        normalized_name="alice cooper",
        mbid=mbid,
        origin=CatalogSource.MUSICBRAINZ if mbid else CatalogSource.LOCAL,
        needs_enhancement=False,
    )


def test_candidates_are_the_works_of_every_artist_of_the_name() -> None:
    artists = FakeArtistRepository()
    works = FakeWorkRepository()
    works.set_artist_repo(artists)
    for artist in (_artist("name-only", None), _artist("band", "mbid-b"), _artist("x", "mbid-s")):
        artists.upsert(artist)
    artists.upsert(Artist(id="other", name="Other", sort_name="Other", normalized_name="other"))
    poison = works.create_local("Poison", "name-only")
    elected = works.create_local("Elected", "band")
    works.create_local("Not Hers", "other")

    candidates = works.get_candidates_by_normalized_artist("alice cooper")

    assert candidates == [(elected, "Elected"), (poison, "Poison")]

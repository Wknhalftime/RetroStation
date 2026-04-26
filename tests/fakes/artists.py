from uuid import uuid4

from backend.domain.catalog import Artist
from backend.domain.enums import CatalogSource
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.artist_enhancement import ArtistEnhancementRepository


class FakeArtistRepository(ArtistCatalogRepository, ArtistEnhancementRepository):
    def __init__(self) -> None:
        self._data: dict[str, Artist] = {}
        # Test observability: every upsert_musicbrainz_artist call is recorded
        # so orchestration tests can assert the SRP-relocated upsert path.
        self.musicbrainz_upserts: list[dict[str, str | None]] = []

    def upsert(self, artist: Artist) -> Artist:
        self._data[artist.id] = artist
        return artist

    def get_by_id(self, artist_id: str) -> Artist | None:
        return self._data.get(artist_id)

    def list_all(self) -> list[Artist]:
        return list(self._data.values())

    def list_unenhanced(self) -> list[Artist]:
        return [
            a for a in self._data.values()
            if a.needs_enhancement and a.enhancement_error is None
        ]

    def mark_enhanced(self, artist_id: str) -> None:
        if artist := self._data.get(artist_id):
            artist.needs_enhancement = False

    def mark_enhancement_failed(self, artist_id: str, error: str) -> None:
        if artist := self._data.get(artist_id):
            artist.enhancement_error = error

    def upsert_local_artist(self, name: str, normalized_name: str) -> str:
        for artist in self._data.values():
            if artist.normalized_name == normalized_name:
                return artist.id
        artist_id = str(uuid4())
        self._data[artist_id] = Artist(
            id=artist_id,
            name=name,
            sort_name=name,
            normalized_name=normalized_name,
            origin=CatalogSource.LOCAL,
            needs_enhancement=False,
        )
        return artist_id

    def upsert_musicbrainz_artist(
        self,
        mbid: str,
        name: str,
        sort_name: str,
        normalized_name: str,
        disambiguation: str | None = None,
    ) -> str:
        self.musicbrainz_upserts.append({
            "mbid": mbid,
            "name": name,
            "sort_name": sort_name,
            "normalized_name": normalized_name,
            "disambiguation": disambiguation,
        })
        # Check by mbid first
        for artist in self._data.values():
            if artist.mbid == mbid:
                return artist.id
        # Check by normalized_name (promote local)
        for artist in self._data.values():
            if artist.normalized_name == normalized_name:
                artist.mbid = mbid
                artist.origin = CatalogSource.MUSICBRAINZ
                artist.name = name
                artist.sort_name = sort_name
                artist.disambiguation = disambiguation
                artist.needs_enhancement = True
                return artist.id
        # Create new MB artist
        artist_id = mbid
        self._data[artist_id] = Artist(
            id=artist_id,
            name=name,
            sort_name=sort_name,
            disambiguation=disambiguation,
            normalized_name=normalized_name,
            mbid=mbid,
            origin=CatalogSource.MUSICBRAINZ,
            needs_enhancement=True,
        )
        return artist_id

    def get_by_normalized_name(self, normalized_name: str) -> Artist | None:
        for artist in self._data.values():
            if artist.normalized_name == normalized_name:
                return artist
        return None

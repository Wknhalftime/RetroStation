from uuid import uuid4

from backend.domain.catalog import Artist
from backend.domain.enums import CatalogSource
from backend.repositories.artists import ArtistRepository
from backend.services.normalization import normalize_artist


class FakeArtistRepository(ArtistRepository):
    def __init__(self) -> None:
        self._data: dict[str, Artist] = {}

    def upsert(self, artist: Artist) -> Artist:
        self._data[artist.id] = artist
        return artist

    def get_by_id(self, mbid: str) -> Artist | None:
        return self._data.get(mbid)

    def fetch_all(self) -> list[Artist]:
        return list(self._data.values())

    def fetch_unenhanced(self) -> list[Artist]:
        return [a for a in self._data.values() if a.needs_enhancement]

    def mark_enhanced(self, mbid: str) -> None:
        if artist := self._data.get(mbid):
            artist.needs_enhancement = False

    def mark_enhancement_failed(self, mbid: str, error: str) -> None:
        if artist := self._data.get(mbid):
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
        disambiguation: str | None = None,
    ) -> str:
        # Check by mbid first
        for artist in self._data.values():
            if artist.mbid == mbid:
                return artist.id
        # Check by normalized_name (promote local)
        norm = normalize_artist(name)
        for artist in self._data.values():
            if artist.normalized_name == norm:
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
            normalized_name=norm,
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

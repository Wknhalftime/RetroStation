from backend.domain.models import Artist
from backend.repositories.artists import ArtistRepository


class FakeArtistRepository(ArtistRepository):
    def __init__(self) -> None:
        self._data: dict[str, Artist] = {}

    def upsert(self, artist: Artist) -> Artist:
        self._data[artist.id] = artist
        return artist

    def get_by_id(self, mbid: str) -> Artist | None:
        return self._data.get(mbid)

    def list_all(self) -> list[Artist]:
        return list(self._data.values())

    def list_needing_enhancement(self) -> list[Artist]:
        return [a for a in self._data.values() if a.needs_enhancement]

    def mark_enhanced(self, mbid: str) -> None:
        if artist := self._data.get(mbid):
            artist.needs_enhancement = False

    def mark_enhancement_failed(self, mbid: str, error: str) -> None:
        if artist := self._data.get(mbid):
            artist.enhancement_error = error

from uuid import uuid4

from backend.domain.catalog import Artist, name_mbids_of
from backend.domain.enums import CatalogSource
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.artist_enhancement import ArtistEnhancementRepository


class FakeArtistRepository(ArtistCatalogRepository, ArtistEnhancementRepository):
    def __init__(self) -> None:
        self._data: dict[str, Artist] = {}
        # Test observability: every upsert_musicbrainz_artist call is recorded
        # so orchestration tests can assert the SRP-relocated upsert path.
        self.musicbrainz_upserts: list[dict[str, str | None]] = []
        # Phase 2a S6: this fake holds no files, so a test sets a name's artist MBID tag values
        # here to reach a shared name. S6, S7 and S14's rules are proved on PostgreSQL.
        self.file_mbids: dict[str, set[str]] = {}

    def upsert(self, artist: Artist) -> Artist:
        self._data[artist.id] = artist
        return artist

    def get_by_id(self, artist_id: str) -> Artist | None:
        return self._data.get(artist_id)

    def get_by_mbid(self, mbid: str) -> Artist | None:
        return next((a for a in self._data.values() if a.mbid == mbid), None)

    def create_stub(self, mbid: str) -> Artist:
        if (held := self.get_by_mbid(mbid)) is not None:
            return held
        stub = Artist(
            id=str(uuid4()),
            name=mbid,
            sort_name=mbid,
            mbid=mbid,
            origin=CatalogSource.MUSICBRAINZ,
            needs_enhancement=True,
        )
        self._data[stub.id] = stub
        return stub

    def list_all(self) -> list[Artist]:
        return [a for a in self._data.values() if a.name != a.mbid]

    def list_unenhanced(self) -> list[Artist]:
        return [
            a for a in self._data.values() if a.needs_enhancement and a.enhancement_error is None
        ]

    def mark_enhanced(self, artist_id: str) -> None:
        if artist := self._data.get(artist_id):
            artist.needs_enhancement = False

    def mark_enhancement_failed(self, artist_id: str, error: str) -> None:
        if artist := self._data.get(artist_id):
            artist.enhancement_error = error

    def name_mbids(self, normalized_name: str) -> set[str]:
        return name_mbids_of(self.file_mbids.get(normalized_name, set()))

    def upsert_local_artist(self, name: str, normalized_name: str) -> str:
        held = [a for a in self._data.values() if a.normalized_name == normalized_name]
        if len(held) == 1 and len(self.name_mbids(normalized_name)) <= 1:
            return held[0].id
        name_only = next((a for a in held if a.mbid is None), None)
        if name_only is not None:
            return name_only.id
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
        self.musicbrainz_upserts.append(
            {
                "mbid": mbid,
                "name": name,
                "sort_name": sort_name,
                "normalized_name": normalized_name,
                "disambiguation": disambiguation,
            }
        )
        # Check by mbid first
        for artist in self._data.values():
            if artist.mbid == mbid:
                return artist.id
        # As PgArtistRepository (phase 2a S6): the name's MBIDs are none or only this one, so the
        # name-only artist takes it; otherwise a new artist, never another MBID's.
        if self.name_mbids(normalized_name) <= {mbid}:
            for artist in self._data.values():
                if artist.normalized_name == normalized_name and artist.mbid is None:
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
        # As PgArtistRepository (phase 2a S5): it answers for the name-only artist.
        for artist in self._data.values():
            if artist.normalized_name == normalized_name and artist.mbid is None:
                return artist
        return None

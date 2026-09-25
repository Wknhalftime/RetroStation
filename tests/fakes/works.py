from uuid import uuid4

from backend.domain.catalog import Work
from backend.domain.enums import CatalogSource
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.works import WorkRepository


class FakeWorkRepository(WorkRepository):
    def __init__(self) -> None:
        self._data: dict[str, Work] = {}
        # Candidate lookup resolves a normalized artist name to an artist id,
        # which is the artist repo's job; tests wire one in via set_artist_repo.
        self._artist_repo: ArtistCatalogRepository | None = None

    def upsert(self, work: Work) -> Work:
        self._data[work.id] = work
        return work

    def get_by_id(self, mbid: str) -> Work | None:
        return self._data.get(mbid)

    def get_by_artist(self, artist_id: str) -> list[Work]:
        return [w for w in self._data.values() if w.artist_id == artist_id]

    def list_needing_enhancement(self) -> list[Work]:
        return [
            w for w in self._data.values()
            if w.needs_enhancement and w.enhancement_error is None
        ]

    def mark_enhanced(self, mbid: str) -> None:
        if work := self._data.get(mbid):
            work.needs_enhancement = False

    def update_embedding(self, mbid: str, embedding: list[float]) -> None:
        if work := self._data.get(mbid):
            work.embedding = embedding

    def create_local(self, title: str, artist_id: str) -> str:
        work_id = str(uuid4())
        self._data[work_id] = Work(
            id=work_id,
            title=title,
            artist_id=artist_id,
            origin=CatalogSource.LOCAL,
            needs_enhancement=False,
        )
        return work_id

    def upsert_from_mb(self, mbid: str, title: str, artist_id: str) -> str:
        for work in self._data.values():
            if work.mbid == mbid:
                return work.id
        work_id = mbid
        self._data[work_id] = Work(
            id=work_id,
            title=title,
            artist_id=artist_id,
            mbid=mbid,
            origin=CatalogSource.MUSICBRAINZ,
            needs_enhancement=True,
        )
        return work_id

    def delete_if_empty(self, work_id: str) -> bool:
        if work_id in self._data:
            del self._data[work_id]
            return True
        return False

    def get_candidates_by_normalized_artist(
        self, normalized_artist_name: str, limit: int = 100,
    ) -> list[tuple[str, str]]:
        # Mirrors PgWorkRepository: resolve normalized artist name → artist_id
        # via the artist repo, then return every work for that artist (including
        # orphan works with no attached library_files).
        if self._artist_repo is None:
            return []
        artist = self._artist_repo.get_by_normalized_name(normalized_artist_name)
        if artist is None:
            return []
        result = [
            (w.id, w.title)
            for w in self._data.values()
            if w.artist_id == artist.id
        ]
        result.sort(key=lambda x: x[1])
        return result[:limit]

    def set_artist_repo(self, repo: ArtistCatalogRepository) -> None:
        """Inject the artist repo the candidate lookup resolves names through."""
        self._artist_repo = repo

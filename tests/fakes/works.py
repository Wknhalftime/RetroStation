from collections.abc import Sequence
from uuid import uuid4

from backend.domain.catalog import EmptyWork, Work
from backend.domain.enums import CatalogSource
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.works import WorkRepository


class FakeWorkRepository(WorkRepository):
    def __init__(self) -> None:
        self._data: dict[str, Work] = {}
        # Candidate lookup resolves a normalized artist name to an artist id,
        # which is the artist repo's job; tests wire one in via set_artist_repo.
        self._artist_repo: ArtistCatalogRepository | None = None
        self._library_file_repo: LibraryFileRepository | None = None

    def upsert(self, work: Work) -> Work:
        self._data[work.id] = work
        return work

    def get_by_id(self, mbid: str) -> Work | None:
        return self._data.get(mbid)

    def get_by_artist(self, artist_id: str) -> list[Work]:
        return [w for w in self._data.values() if w.artist_id == artist_id]

    def list_needing_enhancement(self) -> list[Work]:
        return [
            w for w in self._data.values() if w.needs_enhancement and w.enhancement_error is None
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

    def list_musicbrainz_ids(self, work_ids: Sequence[str]) -> set[str]:
        return {
            w.id
            for w in self._data.values()
            if w.id in set(work_ids) and w.origin == CatalogSource.MUSICBRAINZ
        }

    def get_id_by_mbid(self, mbid: str) -> str | None:
        return next((w.id for w in self._data.values() if w.mbid == mbid), None)

    def claim_mbid(self, work_id: str, mbid: str, title: str) -> bool:
        work = self._data.get(work_id)
        if work is None or work.origin != CatalogSource.LOCAL or work.mbid is not None:
            return False
        if self.get_id_by_mbid(mbid) is not None:
            return False
        work.mbid = mbid
        work.origin = CatalogSource.MUSICBRAINZ
        work.title = title
        work.needs_enhancement = True
        return True

    def drop_fileless_grouping_recordings(self, work_id: str) -> int:
        # The fakes model no recording links to works (delete_if_empty ignores them too).
        return 0

    def delete_if_empty(self, work_id: str) -> bool:
        # Mirrors PgWorkRepository for the links the fakes model: a work that
        # still has files (via the injected library file repo) stays.
        if work_id not in self._data:
            return False
        if self._library_file_repo is not None and self._library_file_repo.get_by_work(work_id):
            return False
        del self._data[work_id]
        return True

    def list_empty_work_ids(self) -> list[str]:
        files = self._library_file_repo
        return sorted(w for w in self._data if files is None or not files.get_by_work(w))

    def kept_empty_works(self) -> list[EmptyWork]:
        # The fakes model no reference but files, so no empty work is kept.
        return []

    def get_candidates_by_normalized_artist(
        self,
        normalized_artist_name: str,
        limit: int = 100,
    ) -> list[tuple[str, str]]:
        # Mirrors PgWorkRepository's join on the name: every work of every artist of the name,
        # with an MBID or not (phase 2a S5), including orphan works with no attached
        # library_files. `get_by_normalized_name` answers only for the name-only artist.
        if self._artist_repo is None:
            return []
        artist_ids = {
            artist.id
            for artist in self._artist_repo.list_all()
            if artist.normalized_name == normalized_artist_name
        }
        result = [(w.id, w.title) for w in self._data.values() if w.artist_id in artist_ids]
        result.sort(key=lambda x: x[1])
        return result[:limit]

    def set_artist_repo(self, repo: ArtistCatalogRepository) -> None:
        """Inject the artist repo the candidate lookup resolves names through."""
        self._artist_repo = repo

    def set_library_file_repo(self, repo: LibraryFileRepository) -> None:
        """Inject the library file repo that delete_if_empty reads a work's files from."""
        self._library_file_repo = repo

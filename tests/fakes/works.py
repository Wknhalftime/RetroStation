from uuid import uuid4

from backend.domain.catalog import Work, WorkFootprint
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
        # Mirrors PgWorkRepository for the links the fakes model: a work that
        # still has files (via the injected library file repo) stays.
        if work_id not in self._data:
            return False
        if self._library_file_repo is not None and self._library_file_repo.get_by_work(work_id):
            return False
        del self._data[work_id]
        return True

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

    def list_local_footprints(self) -> list[WorkFootprint]:
        # Match counts are not modelled by the fakes; file counts come from
        # the injected library file repo when one is wired in.
        return [
            WorkFootprint(
                id=w.id,
                title=w.title,
                artist_id=w.artist_id,
                file_count=(
                    len(self._library_file_repo.get_by_work(w.id))
                    if self._library_file_repo is not None
                    else 0
                ),
            )
            for w in sorted(self._data.values(), key=lambda w: w.id)
            if w.origin == CatalogSource.LOCAL
        ]

    def merge_into(self, target_id: str, source_ids: tuple[str, ...]) -> None:
        # Mirrors PgWorkRepository for the links the fakes model: files move
        # to the target, then the sources are deleted.
        if target_id not in self._data:
            return
        for source_id in source_ids:
            if source_id == target_id or source_id not in self._data:
                continue
            if self._library_file_repo is not None:
                for f in self._library_file_repo.get_by_work(source_id):
                    self._library_file_repo.update_work_id(f.id, target_id)
            del self._data[source_id]

    def set_library_file_repo(self, repo: LibraryFileRepository) -> None:
        """Inject the library file repo that footprints and merges read."""
        self._library_file_repo = repo

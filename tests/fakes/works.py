from uuid import uuid4

from backend.domain.catalog import Work
from backend.domain.enums import CatalogSource
from backend.repositories.works import WorkRepository


class FakeWorkRepository(WorkRepository):
    def __init__(self) -> None:
        self._data: dict[str, Work] = {}

    def upsert(self, work: Work) -> Work:
        self._data[work.id] = work
        return work

    def get_by_id(self, mbid: str) -> Work | None:
        return self._data.get(mbid)

    def get_by_artist(self, artist_id: str) -> list[Work]:
        return [w for w in self._data.values() if w.artist_id == artist_id]

    def list_needing_enhancement(self) -> list[Work]:
        return [w for w in self._data.values() if w.needs_enhancement]

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

    def get_by_mbid(self, mbid: str) -> Work | None:
        for work in self._data.values():
            if work.mbid == mbid:
                return work
        return None

    def delete_if_empty(self, work_id: str) -> bool:
        if work_id in self._data:
            del self._data[work_id]
            return True
        return False

    def get_candidates_by_normalized_artist(
        self, normalized_artist_name: str, limit: int = 100,
    ) -> list[tuple[str, str]]:
        # Need access to library files to filter by artist — use stored ref
        if not hasattr(self, "_library_file_repo"):
            return []
        artist_work_ids: set[str] = set()
        for f in self._library_file_repo._data.values():
            if (
                f.audio.normalized_artist_name == normalized_artist_name
                and f.work_id is not None
            ):
                artist_work_ids.add(f.work_id)
        result = [
            (w.id, w.title)
            for w in self._data.values()
            if w.id in artist_work_ids
        ]
        result.sort(key=lambda x: x[1])
        return result[:limit]

    def set_library_file_repo(self, repo: object) -> None:
        """Inject library file repo reference for candidate lookup."""
        self._library_file_repo = repo  # type: ignore[assignment]

from backend.domain.models import Work
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

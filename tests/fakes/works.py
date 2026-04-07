from uuid import uuid4

from backend.domain.enums import Origin
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

    def create_local(self, title: str, artist_id: str) -> str:
        work_id = str(uuid4())
        self._data[work_id] = Work(
            id=work_id,
            title=title,
            artist_id=artist_id,
            origin=Origin.LOCAL,
            needs_enhancement=False,
        )
        return work_id

    def upsert_from_mb(self, mbid: str, title: str, artist_id: str) -> str:
        for work in self._data.values():
            if work.mbid == mbid:
                return work.id
        work_id = str(uuid4())
        self._data[work_id] = Work(
            id=work_id,
            title=title,
            artist_id=artist_id,
            mbid=mbid,
            origin=Origin.MUSICBRAINZ,
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

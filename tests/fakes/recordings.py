from backend.domain.models import Recording
from backend.repositories.recordings import RecordingRepository


class FakeRecordingRepository(RecordingRepository):
    def __init__(self) -> None:
        self._data: dict[str, Recording] = {}

    def upsert(self, recording: Recording) -> Recording:
        self._data[recording.id] = recording
        return recording

    def get_by_id(self, mbid: str) -> Recording | None:
        return self._data.get(mbid)

    def get_by_work(self, work_id: str) -> list[Recording]:
        return [r for r in self._data.values() if r.work_id == work_id]

    def update_embedding(self, mbid: str, embedding: list[float]) -> None:
        if rec := self._data.get(mbid):
            rec.embedding = embedding

    def list_needing_enhancement(self) -> list[Recording]:
        return [r for r in self._data.values() if r.needs_enhancement]

    def mark_enhanced(self, mbid: str) -> None:
        if rec := self._data.get(mbid):
            rec.needs_enhancement = False

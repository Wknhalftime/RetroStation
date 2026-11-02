from uuid import uuid4

from backend.domain.catalog import Recording
from backend.domain.enums import VersionType
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
        return [
            r for r in self._data.values() if r.needs_enhancement and r.enhancement_error is None
        ]

    def mark_enhanced(self, mbid: str) -> None:
        if rec := self._data.get(mbid):
            rec.needs_enhancement = False

    def update_duration(self, mbid: str, duration_ms: int) -> None:
        if rec := self._data.get(mbid):
            rec.duration_ms = duration_ms

    def _slot_taken(self, rec: Recording, work_id: str | None, version_type: VersionType) -> bool:
        # Mirrors uq_recordings_work_version: one recording per (work, version);
        # a recording on no work clashes with nothing.
        return work_id is not None and any(
            o.work_id == work_id and o.version_type == version_type and o.id != rec.id
            for o in self._data.values()
        )

    def update_version_type(self, mbid: str, version_type: VersionType) -> bool:
        rec = self._data.get(mbid)
        if rec is None or self._slot_taken(rec, rec.work_id, version_type):
            return False
        rec.version_type = version_type
        return True

    def link_work(self, mbid: str, work_id: str) -> bool:
        rec = self._data.get(mbid)
        if rec is None or self._slot_taken(rec, work_id, rec.version_type):
            return False
        rec.work_id = work_id
        return True

    def get_or_create_local(
        self,
        work_id: str,
        version_type: str,
        title: str,
    ) -> str:
        for rec in self._data.values():
            if rec.work_id == work_id and rec.version_type == VersionType(version_type):
                return rec.id
        recording_id = str(uuid4())
        self._data[recording_id] = Recording(
            id=recording_id,
            title=title,
            work_id=work_id,
            version_type=VersionType(version_type),
            needs_enhancement=False,
        )
        return recording_id

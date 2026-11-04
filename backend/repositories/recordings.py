from abc import ABC, abstractmethod

from backend.domain.catalog import Recording
from backend.domain.enums import VersionType


class RecordingRepository(ABC):
    @abstractmethod
    def upsert(self, recording: Recording) -> Recording: ...

    @abstractmethod
    def get_by_id(self, mbid: str) -> Recording | None: ...

    @abstractmethod
    def get_by_work(self, work_id: str) -> list[Recording]: ...

    @abstractmethod
    def update_embedding(self, mbid: str, embedding: list[float]) -> None: ...

    @abstractmethod
    def list_needing_enhancement(self) -> list[Recording]: ...

    @abstractmethod
    def mark_enhanced(self, mbid: str) -> None: ...

    @abstractmethod
    def update_duration(self, mbid: str, duration_ms: int) -> None: ...

    @abstractmethod
    def update_version_type(self, mbid: str, version_type: VersionType) -> bool:
        """Set the recording's version; whether it was set.

        Refused (False) when the recording's work already has a recording of
        that version (``uq_recordings_work_version``). A recording on no work
        has no slot to clash with.
        """
        ...

    @abstractmethod
    def link_work(self, mbid: str, work_id: str) -> bool:
        """Put the recording on ``work_id``; whether it is on it afterwards.

        Refused (False) when the work already has another recording of this
        recording's version (``uq_recordings_work_version``); the recording's
        files can still be moved onto the work. Linking a recording to the
        work it is on is a no-op that returns True.
        """
        ...

    @abstractmethod
    def get_or_create_local(
        self,
        work_id: str,
        version_type: str,
        title: str,
    ) -> str:
        """Return recording id for (work_id, version_type), creating if needed.

        Uses INSERT ON CONFLICT to handle concurrent callers safely.
        Local recordings are created with needs_enhancement=FALSE.
        """
        ...

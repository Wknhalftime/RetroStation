from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.curation import FormatOverride


class FormatOverrideRepository(ABC):
    @abstractmethod
    def create(self, override: FormatOverride) -> FormatOverride: ...

    @abstractmethod
    def get(self, work_id: str, format_name: str) -> FormatOverride | None: ...

    @abstractmethod
    def list_by_work(self, work_id: str) -> list[FormatOverride]: ...

    @abstractmethod
    def delete(self, id: UUID) -> None: ...

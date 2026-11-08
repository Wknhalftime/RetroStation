from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.curation import FormatOverride


class FormatOverrideRepository(ABC):
    @abstractmethod
    def create(self, override: FormatOverride) -> FormatOverride: ...

    @abstractmethod
    def get(self, work_id: str, format_name: str) -> FormatOverride | None: ...

    @abstractmethod
    def list_by_work(self, work_id: str) -> list[FormatOverride]:
        """The work's format overrides; never its version entries (NULL format_name, S3)."""
        ...

    @abstractmethod
    def delete(self, override_id: UUID) -> None: ...

    @abstractmethod
    def delete_for_file(self, file_id: UUID) -> None:
        """Delete every override, of both kinds, whose preferred file this is (E1)."""
        ...

    @abstractmethod
    def move_to_work(self, file_id: UUID, from_work_id: str, to_work_id: str) -> None:
        """Re-key overrides of *from_work_id* that name *file_id* to *to_work_id*.

        Both kinds move, each colliding on its own key: an override *to_work_id*
        already has for the same format, or a version entry for the same version
        text, wins; the moved one is deleted. A version entry that moves copies
        its state to *to_work_id*, combining with one there (dismissed if either
        was, seen at the later time).
        """
        ...

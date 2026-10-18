from abc import ABC, abstractmethod

from backend.domain.streaming import SilentFileEntry


class SilentFileReportRepository(ABC):
    """Read model for the Library Silent Files report (D111, S6)."""

    @abstractmethod
    def candidates(self) -> tuple[SilentFileEntry, ...]:
        """Present files the report may list: at least every one whose ``reasons()`` is not
        empty. An adapter may answer more; the use case keeps those with a reason."""
        ...

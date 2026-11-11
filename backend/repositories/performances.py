"""The performances port (phase 3a PR 2; SPEC S1-S3, S5; design C40, C44, C71).

Performances, their credits, a file's ``performance_id`` and a performance's preferred copy.
Line writes (a stored performance handed off or released) go through ``SongMatchService``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.performances import (
    CopyRow,
    FileForPerformance,
    NewCredit,
    PerformanceCandidate,
    PerformanceKey,
    StoredLine,
)


class PerformanceRepository(ABC):
    # --- reads ---------------------------------------------------------------------------------

    @abstractmethod
    def file_for_performance(self, file_id: UUID) -> FileForPerformance | None: ...

    @abstractmethod
    def candidates(self, work_id: str) -> list[PerformanceCandidate]:
        """The work's performances, with their keys and lowest file ids."""

    @abstractmethod
    def held_files(self, file_paths: list[str]) -> list[tuple[FileForPerformance, PerformanceKey]]:
        """The files at *file_paths* that hold a performance, each with that performance's key."""

    @abstractmethod
    def artists_by_mbid(self, mbids: list[str]) -> dict[str, tuple[str, str]]:
        """(artist id, name) of the artist holding each MBID that one holds."""

    @abstractmethod
    def work_of(self, performance_id: UUID) -> str | None:
        """The performance's work, or None when it is gone."""

    @abstractmethod
    def file_count(self, performance_id: UUID) -> int: ...

    @abstractmethod
    def copies(self, performance_id: UUID) -> list[CopyRow]: ...

    @abstractmethod
    def preferred_copy(self, performance_id: UUID) -> UUID | None: ...

    @abstractmethod
    def stored_lines(self, performance_id: UUID) -> list[StoredLine]: ...

    @abstractmethod
    def files_waiting(self, after: UUID | None, limit: int) -> list[UUID]:
        """Files on a work with no performance, in id order, after *after*."""

    @abstractmethod
    def stranded_preferred_copies(self) -> list[UUID]:
        """Performances whose preferred copy is missing."""

    # --- writes --------------------------------------------------------------------------------

    @abstractmethod
    def create(self, work_id: str, key: PerformanceKey, credits: list[NewCredit]) -> UUID: ...

    @abstractmethod
    def hold(self, file_id: UUID, performance_id: UUID | None) -> None:
        """Set the file's ``performance_id``."""

    @abstractmethod
    def set_preferred_copy(self, performance_id: UUID, file_id: UUID | None) -> None: ...

    @abstractmethod
    def delete(self, performance_id: UUID) -> None: ...

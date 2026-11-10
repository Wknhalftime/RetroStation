"""The version pages' port: version entries and their states, with the matched lines they come
from (versions phase 2b PR 3, SPEC S5, S7, S8; design C60, C62)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.version_pages import VersionEntryRow, WorkFile


class VersionEntryRepository(ABC):
    """Reads the (work, version text) rows of matched lines and entries; writes entries and
    states. Writes never check: the service has."""

    @abstractmethod
    def entry_rows(self, work_ids: list[str] | None) -> list[VersionEntryRow]:
        """Every (work, text) on the given works' matched lines or entries; None: every work."""

    @abstractmethod
    def line_files(self, work_ids: list[str] | None) -> dict[tuple[str, str], frozenset[UUID]]:
        """The best-match files of each (work, text)'s matched lines."""

    @abstractmethod
    def work_files(self, work_ids: list[str]) -> dict[str, list[WorkFile]]:
        """Each work's present files (its own work_id), by work."""

    @abstractmethod
    def group_work_ids(self, artist_id: str) -> list[str] | None:
        """The works of the artist's merge group; None when the artist doesn't exist."""

    @abstractmethod
    def map_texts(self, work_id: str, texts: list[str], file_id: UUID) -> None:
        """Point each text's entry at the file, and mark each seen now with no dismissal."""

    @abstractmethod
    def unmap_texts(self, work_id: str, texts: list[str]) -> None:
        """Delete each text's entry; states stay."""

    @abstractmethod
    def dismiss(self, keys: list[tuple[str, str]]) -> None:
        """Mark each (work, text) dismissed, seen now."""

"""The global recording index's port (work merges phase 3b PR 4; SPEC S5; design C40)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from backend.domain.performances import RecordingIndexState


class RecordingIndexRepository(ABC):
    @abstractmethod
    def state(self) -> RecordingIndexState:
        """Whether the index exists, and whether it is valid."""

    @abstractmethod
    def drop(self) -> None:
        """Drop the index, concurrently."""

    @abstractmethod
    def build(self) -> None:
        """Build the index concurrently, outside a transaction; a duplicate MBID met on the way
        raises RecordingIndexDuplicateError (and leaves an invalid index)."""

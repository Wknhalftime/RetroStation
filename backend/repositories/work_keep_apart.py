"""The "Keep apart" marks (work merges phase 3b PR 5c; SPEC D40, D55, D57; scope check Q3, Q4,
Q11; AUD-R089): pairs of works the merge planner must never join to each other."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from backend.domain.merge_list import KeptApart
from backend.domain.work_merge_planner import WorkPair


class WorkKeepApartRepository(ABC):
    @abstractmethod
    def keep(self, pairs: Sequence[WorkPair], note: str) -> None:
        """Keep each pair apart, noted *note*; a pair already kept keeps its row."""

    @abstractmethod
    def undo(self, pair: WorkPair) -> None:
        """Keep *pair* apart no more; nothing when it isn't kept (Q3)."""

    @abstractmethod
    def marks(self) -> list[KeptApart]:
        """Every kept pair, newest first, then by pair."""

    @abstractmethod
    def pairs_among(self, work_ids: Sequence[str]) -> list[WorkPair]:
        """The kept pairs both of whose works are among *work_ids*, in order."""

    @abstractmethod
    def carry(self, source_ids: Sequence[str], target_id: str) -> None:
        """A merge of *source_ids* into *target_id* (Q4): each source's marks re-pointed to the
        target, a pair joining the target to itself dropped (D57), and of two marks landing on
        one pair the one kept earlier staying, with its note."""

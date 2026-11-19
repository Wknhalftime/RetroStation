"""The guarded merge's port: the group's lines through the air-play view, the savepoint, the
merge log and redirects (phase 3b PR 3; AUD-R079)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from backend.domain.work_merges import LineKey, LineResult, WorkMergeGroup


@dataclass(frozen=True)
class MasterLine:
    """A line of a work that reaches its master step somewhere: its best match, the master's
    file it plays, and its log name's active links."""

    match_id: UUID
    identity_id: UUID
    master_file_id: UUID
    artists: frozenset[str]


class WorkMergeRepository(ABC):
    @abstractmethod
    def lines_of_works(self, work_ids: Sequence[str]) -> list[UUID]:
        """The lines whose view work is one of *work_ids*."""

    @abstractmethod
    def line_results(self, identity_ids: Sequence[UUID]) -> dict[LineKey, LineResult]:
        """What each (station, line) of *identity_ids* plays now."""

    @abstractmethod
    def master_lines(self, work_ids: Sequence[str]) -> list[MasterLine]:
        """The lines of *work_ids* that play a master at some station."""

    @abstractmethod
    def in_savepoint[T](self, attempt: Callable[[], tuple[T, bool]]) -> T:
        """Run *attempt* in a savepoint, kept when it answers True, else undone; a database
        error undoes it and raises WorkMergeStorageError."""

    @abstractmethod
    def start(self, group: WorkMergeGroup) -> UUID:
        """Log the merge as merged and capture the rows it writes from now on."""

    @abstractmethod
    def stop(self) -> None:
        """Stop capturing."""

    @abstractmethod
    def redirect(self, old_ids: Sequence[str], new_id: str, merge_id: UUID) -> None:
        """*old_ids* and every id already redirected to them now redirect to *new_id*."""

    @abstractmethod
    def log_failure(self, group: WorkMergeGroup, error: str) -> UUID: ...

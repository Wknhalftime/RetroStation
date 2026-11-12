"""The Credit picks port: ``credit_pick_proposals`` and the reads its rules need (phase 3a PR 3,
SPEC S8, S9). It never writes ``matches``: storing a performance goes through
``SongMatchService`` (C52)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from backend.domain.credit_picks import (
    LineCredit,
    LineNow,
    Proposal,
    ProposalAnswer,
    ProposalState,
    WorkCredits,
)


@dataclass(frozen=True)
class FileShown:
    """A file as the page names it."""

    file_id: UUID
    file_path: str
    track_title: str | None
    file_status: str


@dataclass(frozen=True)
class LineShown:
    """A line as the page shows it: its title, log name, plays, work and what it plays now (the
    air-play view at its latest play)."""

    identity_id: UUID
    title: str
    artist_name: str
    plays: int
    work_id: str | None
    work_title: str | None
    plays_now_file_id: UUID | None
    plays_now_source: str | None


class CreditPickRepository(ABC):
    # --- what the proposals read ---------------------------------------------------------------

    @abstractmethod
    def lines_to_propose(self) -> list[LineCredit]:
        """Every matched line (the view's rule) on a work whose log name has an active link."""

    @abstractmethod
    def work_credits(self, work_ids: Sequence[str]) -> dict[str, WorkCredits]: ...

    @abstractmethod
    def primaries(self, artist_ids: Iterable[str]) -> dict[str, str]:
        """Each merged artist among *artist_ids* and its group's root primary."""

    # --- the table -----------------------------------------------------------------------------

    @abstractmethod
    def all_proposals(self) -> list[Proposal]: ...

    @abstractmethod
    def proposals(self, ids: Sequence[UUID]) -> dict[UUID, Proposal]:
        """The rows named, locked for the caller's transaction."""

    @abstractmethod
    def listed(self) -> list[Proposal]:
        """The undecided rows, and the picked version lines and ties."""

    @abstractmethod
    def insert(self, identity_id: UUID, answer: ProposalAnswer) -> UUID: ...

    @abstractmethod
    def delete(self, ids: Sequence[UUID]) -> None: ...

    @abstractmethod
    def decide(
        self, ids: Sequence[UUID], state: ProposalState, performance_id: UUID | None = None
    ) -> None:
        """Mark the rows decided now; a pick also records the performance picked."""

    # --- what Apply, a pick and the page read --------------------------------------------------

    @abstractmethod
    def lines_now(self, identity_ids: Sequence[UUID]) -> dict[UUID, LineNow]: ...

    @abstractmethod
    def performance_works(self, performance_ids: Iterable[UUID]) -> dict[UUID, str]: ...

    @abstractmethod
    def lines_shown(self, identity_ids: Sequence[UUID]) -> dict[UUID, LineShown]: ...

    @abstractmethod
    def files_shown(self, file_ids: Iterable[UUID]) -> dict[UUID, FileShown]: ...

    @abstractmethod
    def credit_names(self, performance_ids: Iterable[UUID]) -> dict[UUID, list[str]]:
        """Each performance's credited names, in credit order."""

from abc import ABC, abstractmethod
from uuid import UUID

from backend.domain.matching import Match


class MatchRepository(ABC):
    @abstractmethod
    def create(self, match: Match) -> Match: ...

    @abstractmethod
    def get_by_identity(self, identity_id: UUID) -> Match | None: ...

    @abstractmethod
    def delete_for_identity(self, identity_id: UUID) -> None: ...

    @abstractmethod
    def delete_for_file(self, file_id: UUID) -> list[UUID]:
        """Delete every match naming the file; the identity id of each match deleted."""
        ...

    # Version-entry states follow a file's matched lines (phase 2b, D6). They live on this port
    # because the lines are found through matches, and every path that moves a file's lines
    # (fold, split, reassign, move_file_to_work) already holds it. A line matched to a file is
    # the view's: auto or manual matched, with that file as its best match.

    @abstractmethod
    def copy_version_states(self, file_id: UUID, from_work_id: str, to_work_id: str) -> None:
        """Copy *from_work_id*'s state of each version text on a line matched to *file_id* to
        *to_work_id* (the fold: one song). Where *to_work_id* has one: dismissed if either
        was, seen at the later time. *from_work_id* keeps its own."""
        ...

    @abstractmethod
    def see_version_states(self, file_id: UUID, from_work_id: str, to_work_id: str) -> None:
        """Give *to_work_id* each version text on a line matched to *file_id* that
        *from_work_id* has a state for, as seen at that ``seen_at``, never a dismissal (split,
        reassign, ``move_file_to_work``: another song). A state *to_work_id* has keeps its own
        dismissal or none, seen at the later time. *from_work_id* keeps its own."""
        ...

    @abstractmethod
    def move_to_work(self, file_id: UUID, work_id: str | None) -> None:
        """Point every match on the file at the work the file now belongs to.

        ``matches.work_id`` mirrors ``library_files.work_id`` for the matched
        file, so it follows the file whenever the file changes work.
        """
        ...

    # The works merge's and a left work's statements (moved from the curation port in phase
    # 3a PR 1, D2: only SongMatchService writes matches).

    @abstractmethod
    def move_matches_of_works(self, work_ids: list[str], target_id: str) -> None:
        """Point every match at *work_ids*, as its target or its file's work, at *target_id*."""
        ...

    @abstractmethod
    def release_work_matches(self, work_id: str) -> None:
        """Point each match still on *work_id* at its file's current work (or none)."""
        ...

    @abstractmethod
    def set_stored_performance(self, match_ids: list[UUID], performance_id: UUID | None) -> None:
        """The performance the matches store: another (a hand-off) or none (a release)."""
        ...

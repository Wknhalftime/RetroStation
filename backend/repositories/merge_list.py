"""The merge list's reads (work merges phase 3b PR 4; SPEC S1, S2, E3, E5; AUD-R080)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from backend.domain.merge_list import LoggedFailure, RejectingLine, WorkView


@dataclass(frozen=True)
class PlanCredit:
    """One credit of a performance of the work: its artist (by id, when one holds it), its MBID
    (the credit's, else its artist's) and its names (credited, and the artist's own)."""

    artist_id: str | None
    mbid: str | None
    names: tuple[str, ...]


@dataclass(frozen=True)
class PlanWork:
    """What the planner needs of a work, as stored."""

    work_id: str
    title: str
    work_mbid: str | None
    linked_mbids: frozenset[str]
    credits: tuple[PlanCredit, ...]


class MergeListRepository(ABC):
    @abstractmethod
    def plan_works(self) -> list[PlanWork]:
        """Every work, with its MusicBrainz work MBID (a MusicBrainz work's), the work MBIDs its
        performances' cached recording lookups name, and its performances' credits."""

    @abstractmethod
    def primaries(self, artist_ids: Iterable[str]) -> dict[str, str]:
        """Each of *artist_ids* in an artist merge -> its group's primary (2a)."""

    @abstractmethod
    def matched_plays(self, work_ids: Sequence[str]) -> dict[str, int]:
        """The plays of the lines whose view work is each work (3b D9's count)."""

    @abstractmethod
    def works_shown(self, work_ids: Sequence[str]) -> dict[str, WorkView]:
        """Each work as the list shows it (plays left at 0: the caller has them)."""

    @abstractmethod
    def rejections(self, work_ids: Sequence[str]) -> list[RejectingLine]:
        """Each line that rejected a file of one of *work_ids*, by line then work."""

    @abstractmethod
    def failures(self, target_ids: Sequence[str]) -> list[LoggedFailure]:
        """The merge log's failed merges into *target_ids*, newest first."""

    @abstractmethod
    def live_ids(self, work_ids: Sequence[str]) -> dict[str, str]:
        """Each of *work_ids* that is a work, or redirects to one -> that work."""

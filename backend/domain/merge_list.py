"""The merge list (work merges phase 3b PR 4; SPEC S2, D6, D15, D26-D28, E3, E5; scope Q11-Q13;
design C57, C74; AUD-R080). Pure: what the page shows of each planned group and what Apply did.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from backend.domain.catalog import EmptyWork
from backend.domain.work_merge_planner import MergeJoin
from backend.domain.work_merges import MergeApproval, MergeChange


class FlagKind(StrEnum):
    """What a group shows but never blocks on (Q11)."""

    MASTERS_MEET = "masters_meet"
    CUE_EDIT = "cue_edit"
    REJECTION = "rejection"


@dataclass(frozen=True)
class GroupFlag:
    kind: FlagKind
    work_id: str | None
    file_id: UUID | None
    identity_id: UUID | None
    text: str


@dataclass(frozen=True)
class MasterView:
    file_id: UUID
    file_path: str | None
    method: str


@dataclass(frozen=True)
class WorkView:
    """A work as the list shows it: its credited artists, plays and master."""

    work_id: str
    title: str
    artists: tuple[str, ...]
    plays: int
    musicbrainz: bool
    master: MasterView | None
    cue_edited: bool = False


@dataclass(frozen=True)
class RejectingLine:
    """A line that rejected a file of *work_id* (E3)."""

    identity_id: UUID
    title: str
    artist: str
    work_id: str


@dataclass(frozen=True)
class LoggedFailure:
    """A failed merge in the merge log (0054)."""

    target_id: str
    source_ids: frozenset[str]
    error: str
    logged_at: datetime


@dataclass(frozen=True)
class GroupFailure:
    """A failed attempt at a group: one the merge log holds (with its time), or the page's own
    preview (no time: the preview logs nothing, D27)."""

    error: str
    logged_at: datetime | None


@dataclass(frozen=True)
class GroupView:
    target_id: str
    source_ids: tuple[str, ...]
    joins: tuple[MergeJoin, ...]
    plays: int
    works: tuple[WorkView, ...]
    flags: tuple[GroupFlag, ...]
    changes: tuple[MergeChange, ...]
    failed: GroupFailure | None


@dataclass(frozen=True)
class NamedWork:
    work_id: str
    title: str


@dataclass(frozen=True)
class DuplicateView:
    """A recording MBID still under two works (S5)."""

    recording_mbid: str
    works: tuple[NamedWork, ...]


@dataclass(frozen=True)
class MergeListPage:
    """One page of the planned groups (D37), with *total* counting every planned group, and the
    other two sections whole."""

    groups: tuple[GroupView, ...]
    duplicates: tuple[DuplicateView, ...]
    kept_works: tuple[EmptyWork, ...]
    total: int
    offset: int
    limit: int


PAGE_SIZE = 50  # D37: groups a page, and the most one page holds


@dataclass(frozen=True)
class MergeListCount:
    """Library Status's count (D28): the planned groups, and the other sections' rows."""

    total: int
    duplicates: int
    kept_works: int


@dataclass(frozen=True)
class SeenGroup:
    """A group as Lance saw it and didn't skip, with the changes he applied there (D6)."""

    target_id: str
    source_ids: tuple[str, ...]
    approvals: frozenset[MergeApproval]


class GroupOutcome(StrEnum):
    MERGED = "merged"
    WAITING = "waiting"
    FAILED = "failed"
    ALREADY_MERGED = "already_merged"
    CHANGED = "changed"


@dataclass(frozen=True)
class GroupResult:
    target_id: str
    source_ids: tuple[str, ...]
    outcome: GroupOutcome
    changes: tuple[MergeChange, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class ApplyReport:
    results: tuple[GroupResult, ...]

    def count(self, outcome: GroupOutcome) -> int:
        return sum(1 for r in self.results if r.outcome is outcome)


def group_flags(
    works: Sequence[WorkView], rejections: Sequence[RejectingLine]
) -> tuple[GroupFlag, ...]:
    """A group's flags, in this order: each hand-picked master that meets another work's master,
    each master whose audio holds a manual cue edit, each line that rejected a file of one of
    its works (4.3 widens it to the merged work, E3)."""
    mastered = [w for w in works if w.master is not None]
    flags: list[GroupFlag] = [
        GroupFlag(
            FlagKind.MASTERS_MEET,
            w.work_id,
            w.master.file_id,
            None,
            f"Hand-picked master of {w.title} meets another work's master",
        )
        for w in mastered
        if w.master is not None and w.master.method == "manual" and len(mastered) > 1
    ]
    flags += [
        GroupFlag(
            FlagKind.CUE_EDIT,
            w.work_id,
            w.master.file_id,
            None,
            f"The master of {w.title} has a cue edit",
        )
        for w in mastered
        if w.master is not None and w.cue_edited
    ]
    flags += [
        GroupFlag(
            FlagKind.REJECTION,
            r.work_id,
            None,
            r.identity_id,
            f"{r.title} ({r.artist}) rejected a copy; merged, the rejection reaches the whole work",
        )
        for r in rejections
    ]
    return tuple(flags)


def failure_of(
    target_id: str,
    source_ids: Sequence[str],
    logged: Sequence[LoggedFailure],
    preview_error: str | None,
) -> GroupFailure | None:
    """The latest logged failure merging any of *source_ids* into *target_id* (*logged* newest
    first), else the preview's own error."""
    sources = set(source_ids)
    for failure in logged:
        if failure.target_id == target_id and sources & failure.source_ids:
            return GroupFailure(failure.error, failure.logged_at)
    return GroupFailure(preview_error, None) if preview_error is not None else None


def works_in_order(
    target_id: str, source_ids: Sequence[str], shown: Mapping[str, WorkView]
) -> tuple[WorkView, ...]:
    return tuple(shown[w] for w in (target_id, *source_ids) if w in shown)

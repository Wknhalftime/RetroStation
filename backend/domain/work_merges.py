"""The guarded work merge (phase 3b PR 3; SPEC S3, S4, D2, D3; design C58, C74; AUD-R079).

Pure rules only: what a merge changed in air play, and whether Lance approved it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from backend.domain.curation import MergeResult
from backend.domain.work_merge_planner import WorkPair


class WorkMergeError(Exception):
    """Base for the guarded merge's errors."""


class WorkMergeStorageError(WorkMergeError):
    """The database refused part of a merge."""


class WorkMergeFailedError(WorkMergeError):
    """A merge failed partway; it was undone and logged as failed."""


class MergeReason(StrEnum):
    LIBRARY = "library"
    MERGE_LIST = "merge list"
    WORK_LINK = "musicbrainz work link"


@dataclass(frozen=True)
class MergeApproval:
    """Lance's approval of one change: a (station, line) may play *file_id* (Q8)."""

    station_id: UUID
    identity_id: UUID
    file_id: UUID | None


@dataclass(frozen=True)
class LineResult:
    """What a line plays at a station, as the air-play view resolves it, and how it shows."""

    station_id: UUID
    station: str
    identity_id: UUID
    title: str
    artist: str
    plays: int
    file_id: UUID | None
    path: str | None


@dataclass(frozen=True)
class MergeChange:
    """A (station, line) whose file a merge changes."""

    station_id: UUID
    station: str
    identity_id: UUID
    title: str
    artist: str
    plays: int
    from_file_id: UUID | None
    from_path: str | None
    to_file_id: UUID | None
    to_path: str | None

    @property
    def approval(self) -> MergeApproval:
        return MergeApproval(self.station_id, self.identity_id, self.to_file_id)


@dataclass(frozen=True)
class WorkMergeGroup:
    """One merge: the sources into the target, why, and what Lance approved."""

    target_id: str
    source_ids: tuple[str, ...]
    reason: MergeReason
    approvals: frozenset[MergeApproval] = frozenset()


@dataclass(frozen=True)
class MergeDone:
    merge_id: UUID
    result: MergeResult
    changes: tuple[MergeChange, ...]


@dataclass(frozen=True)
class MergeRefused:
    """The merge was undone: these changes wait for Lance's approval (C74). Or it never ran:
    the group holds works Lance keeps apart (PR 5c, Q6), named in ``kept_apart``."""

    changes: tuple[MergeChange, ...]
    kept_apart: tuple[WorkPair, ...] = ()


type LineKey = tuple[UUID, UUID]


def changes_between(
    before: Mapping[LineKey, LineResult], after: Mapping[LineKey, LineResult]
) -> tuple[MergeChange, ...]:
    """Every (station, line) of *before* whose file differs in *after*, in a stable order."""
    found: list[MergeChange] = []
    for key, was in before.items():
        now = after.get(key)
        to_file = now.file_id if now is not None else None
        if to_file == was.file_id:
            continue
        found.append(
            MergeChange(
                station_id=was.station_id,
                station=was.station,
                identity_id=was.identity_id,
                title=was.title,
                artist=was.artist,
                plays=was.plays,
                from_file_id=was.file_id,
                from_path=was.path,
                to_file_id=to_file,
                to_path=now.path if now is not None else None,
            )
        )
    return tuple(sorted(found, key=lambda c: (c.station, c.title, str(c.identity_id))))


def all_approved(changes: Iterable[MergeChange], approvals: frozenset[MergeApproval]) -> bool:
    return all(c.approval in approvals for c in changes)

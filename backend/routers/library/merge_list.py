"""The merge list: the Library page of planned work merges, its count and Apply (work merges
phase 3b PR 4; SPEC S2, D6, D15, D26-D28, E3, E5; scope Q11-Q13, Q22; AUD-R080), and Lance's
"Keep apart" marks (PR 5c; SPEC D40, D55, D56, D58; AUD-R089)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.dependencies import get_current_token, get_merge_list_commit, get_merge_list_repos
from backend.domain.merge_list import (
    PAGE_SIZE,
    ApplyReport,
    GroupFlag,
    GroupOutcome,
    GroupView,
    KeepApartTooFewWorksError,
    KeepApartWorkNotFoundError,
    KeptApartView,
    SeenGroup,
    WorkView,
)
from backend.domain.work_merge_planner import CreditsNotReadyError, WorkPair
from backend.domain.work_merges import MergeApproval, MergeChange
from backend.services import merge_list_service as service
from backend.services.merge_list_service import MergeListRepos

router = APIRouter()

Token = Annotated[str, Depends(get_current_token)]
Repos = Annotated[MergeListRepos, Depends(get_merge_list_repos)]
Commit = Annotated[Callable[[], None], Depends(get_merge_list_commit)]


class MasterInfo(BaseModel):
    file_id: UUID
    file_path: str | None
    method: str


class WorkInfo(BaseModel):
    work_id: str
    title: str
    artists: list[str]
    plays: int
    musicbrainz: bool
    master: MasterInfo | None


class FlagInfo(BaseModel):
    kind: str
    work_id: str | None
    file_id: UUID | None
    identity_id: UUID | None
    text: str


class ChangeInfo(BaseModel):
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


class FailureInfo(BaseModel):
    error: str
    logged_at: datetime | None


class PairInfo(BaseModel):
    first_work_id: str
    second_work_id: str


class GroupInfo(BaseModel):
    target_id: str
    source_ids: list[str]
    joins: list[str]
    plays: int
    works: list[WorkInfo]
    flags: list[FlagInfo]
    changes: list[ChangeInfo]
    failed: FailureInfo | None
    held: list[PairInfo]


class KeptWorkNameInfo(BaseModel):
    work_id: str
    title: str
    artists: list[str]


class KeptApartInfo(BaseModel):
    first: KeptWorkNameInfo
    second: KeptWorkNameInfo
    kept_at: datetime
    note: str
    musicbrainz_links: bool


class NamedWorkInfo(BaseModel):
    work_id: str
    title: str


class DuplicateInfo(BaseModel):
    recording_mbid: str
    works: list[NamedWorkInfo]


class KeptWorkInfo(BaseModel):
    work_id: str
    title: str
    references: list[str]


class MergeListPage(BaseModel):
    groups: list[GroupInfo]
    duplicates: list[DuplicateInfo]
    kept_works: list[KeptWorkInfo]
    kept_apart: list[KeptApartInfo]
    total: int
    offset: int
    limit: int


class MergeListCount(BaseModel):
    total: int
    duplicates: int
    kept_works: int
    kept_apart: int


class KeepApartRequest(BaseModel):
    work_ids: list[str] = Field(min_length=2)


class KeepApartResult(BaseModel):
    kept: list[PairInfo]


class UndoKeepApartRequest(BaseModel):
    first_work_id: str
    second_work_id: str


class ApprovalInfo(BaseModel):
    station_id: UUID
    identity_id: UUID
    file_id: UUID | None


class SeenGroupInfo(BaseModel):
    target_id: str
    source_ids: list[str] = Field(min_length=1)
    approvals: list[ApprovalInfo] = []


class ApplyRequest(BaseModel):
    groups: list[SeenGroupInfo]


class GroupResultInfo(BaseModel):
    target_id: str
    source_ids: list[str]
    outcome: str
    changes: list[ChangeInfo]
    error: str | None


class ApplyResult(BaseModel):
    results: list[GroupResultInfo]
    merged: int
    waiting: int
    failed: int
    already_merged: int
    changed: int


def _change(c: MergeChange) -> ChangeInfo:
    return ChangeInfo(
        station_id=c.station_id,
        station=c.station,
        identity_id=c.identity_id,
        title=c.title,
        artist=c.artist,
        plays=c.plays,
        from_file_id=c.from_file_id,
        from_path=c.from_path,
        to_file_id=c.to_file_id,
        to_path=c.to_path,
    )


def _work(w: WorkView) -> WorkInfo:
    master = (
        None
        if w.master is None
        else MasterInfo(
            file_id=w.master.file_id, file_path=w.master.file_path, method=w.master.method
        )
    )
    return WorkInfo(
        work_id=w.work_id,
        title=w.title,
        artists=list(w.artists),
        plays=w.plays,
        musicbrainz=w.musicbrainz,
        master=master,
    )


def _flag(f: GroupFlag) -> FlagInfo:
    return FlagInfo(
        kind=f.kind.value,
        work_id=f.work_id,
        file_id=f.file_id,
        identity_id=f.identity_id,
        text=f.text,
    )


def _group(g: GroupView) -> GroupInfo:
    return GroupInfo(
        target_id=g.target_id,
        source_ids=list(g.source_ids),
        joins=[j.value for j in g.joins],
        plays=g.plays,
        works=[_work(w) for w in g.works],
        flags=[_flag(f) for f in g.flags],
        changes=[_change(c) for c in g.changes],
        failed=(
            None
            if g.failed is None
            else FailureInfo(error=g.failed.error, logged_at=g.failed.logged_at)
        ),
        held=[_pair(p) for p in g.held],
    )


def _pair(p: WorkPair) -> PairInfo:
    return PairInfo(first_work_id=p.first_work_id, second_work_id=p.second_work_id)


def _named(w: WorkView) -> KeptWorkNameInfo:
    return KeptWorkNameInfo(work_id=w.work_id, title=w.title, artists=list(w.artists))


def _kept(k: KeptApartView) -> KeptApartInfo:
    return KeptApartInfo(
        first=_named(k.first),
        second=_named(k.second),
        kept_at=k.kept_at,
        note=k.note,
        musicbrainz_links=k.musicbrainz_links,
    )


def _not_ready(refused: CreditsNotReadyError) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(refused))


@router.get("/merge-list", response_model=MergeListPage)
def get_merge_list(
    _token: Token,
    repos: Repos,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=PAGE_SIZE)] = PAGE_SIZE,
) -> MergeListPage:
    """One page of the planned groups (D37), the recordings under two works, the empty works
    kept (S2, D15)."""
    try:
        page = service.page(repos, offset, limit)
    except CreditsNotReadyError as refused:
        raise _not_ready(refused) from refused
    return MergeListPage(
        groups=[_group(g) for g in page.groups],
        duplicates=[
            DuplicateInfo(
                recording_mbid=d.recording_mbid,
                works=[NamedWorkInfo(work_id=w.work_id, title=w.title) for w in d.works],
            )
            for d in page.duplicates
        ],
        kept_works=[
            KeptWorkInfo(
                work_id=k.work_id, title=k.title, references=[r.value for r in k.references]
            )
            for k in page.kept_works
        ],
        kept_apart=[_kept(k) for k in page.kept_apart],
        total=page.total,
        offset=page.offset,
        limit=page.limit,
    )


@router.get("/merge-list/count", response_model=MergeListCount)
def get_merge_list_count(_token: Token, repos: Repos) -> MergeListCount:
    """Library Status's count (D28)."""
    try:
        counted = service.count(repos)
    except CreditsNotReadyError as refused:
        raise _not_ready(refused) from refused
    return MergeListCount(
        total=counted.total,
        duplicates=counted.duplicates,
        kept_works=counted.kept_works,
        kept_apart=counted.kept_apart,
    )


@router.post("/merge-list/keep-apart", response_model=KeepApartResult)
def keep_apart(_token: Token, body: KeepApartRequest, repos: Repos) -> KeepApartResult:
    """Keep every pair of a group's works apart (D55)."""
    try:
        pairs = service.keep_apart(body.work_ids, repos)
    except KeepApartWorkNotFoundError as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from missing
    except KeepApartTooFewWorksError as few:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(few)) from few
    return KeepApartResult(kept=[_pair(p) for p in pairs])


@router.post("/merge-list/kept-apart/undo", status_code=status.HTTP_204_NO_CONTENT)
def undo_keep_apart(_token: Token, body: UndoKeepApartRequest, repos: Repos) -> None:
    """Keep one pair apart no more (D55); a pair that isn't kept is no error (Q3)."""
    service.undo_keep_apart(body.first_work_id, body.second_work_id, repos)


def _seen(g: SeenGroupInfo) -> SeenGroup:
    return SeenGroup(
        target_id=g.target_id,
        source_ids=tuple(g.source_ids),
        approvals=frozenset(
            MergeApproval(a.station_id, a.identity_id, a.file_id) for a in g.approvals
        ),
    )


def _result(report: ApplyReport) -> ApplyResult:
    return ApplyResult(
        results=[
            GroupResultInfo(
                target_id=r.target_id,
                source_ids=list(r.source_ids),
                outcome=r.outcome.value,
                changes=[_change(c) for c in r.changes],
                error=r.error,
            )
            for r in report.results
        ],
        merged=report.count(GroupOutcome.MERGED),
        waiting=report.count(GroupOutcome.WAITING),
        failed=report.count(GroupOutcome.FAILED),
        already_merged=report.count(GroupOutcome.ALREADY_MERGED),
        changed=report.count(GroupOutcome.CHANGED),
    )


@router.post("/merge-list/apply", response_model=ApplyResult)
def apply_merge_list(
    _token: Token, body: ApplyRequest, repos: Repos, commit: Commit
) -> ApplyResult:
    """Merge the groups Lance saw, minus his skips, with the changes he applied (S2, D26)."""
    try:
        report = service.apply([_seen(g) for g in body.groups], repos, commit)
    except CreditsNotReadyError as refused:
        raise _not_ready(refused) from refused
    return _result(report)

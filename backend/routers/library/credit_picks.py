"""Credit picks: the Library page of proposed stored performances, its Apply and its version-line
pick (performances phase 3a PR 3, SPEC S9, E6, D7; design C57, C64, C65; AUD-R039, AUD-R076)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from backend.dependencies import get_credit_pick_repos, get_current_token
from backend.domain.credit_picks import (
    CandidateNotListedError,
    ProposalKindError,
    ProposalNotFoundError,
    ProposalStaleError,
    ProposalTwiceError,
)
from backend.services import credit_pick_service as service
from backend.services.credit_pick_service import CreditPickRepos, FileView, ProposalView

router = APIRouter()

Token = Annotated[str, Depends(get_current_token)]
Repos = Annotated[CreditPickRepos, Depends(get_credit_pick_repos)]


class FileInfo(BaseModel):
    file_id: UUID
    file_path: str
    track_title: str | None
    file_status: str
    source: str | None


class CandidateInfo(BaseModel):
    performance_id: UUID
    credit_names: list[str]
    would_play: FileInfo | None


class ProposalInfo(BaseModel):
    id: UUID
    identity_id: UUID
    kind: str
    reason: str
    state: str
    title: str
    artist_name: str
    plays: int
    work_id: str | None
    work_title: str | None
    performance_id: UUID | None
    plays_now: FileInfo | None
    would_play: FileInfo | None
    candidates: list[CandidateInfo]


class CreditPicksPage(BaseModel):
    credit: list[ProposalInfo]
    version_lines: list[ProposalInfo]
    total: int


class CreditPicksCount(BaseModel):
    total: int


class ApplyRequest(BaseModel):
    apply: list[UUID]
    skip: list[UUID]


class ApplyResult(BaseModel):
    applied: int
    skipped: int
    stale: list[UUID]


class PickRequest(BaseModel):
    performance_id: UUID | None


class PickResult(BaseModel):
    performance_id: UUID | None


def _file(view: FileView | None) -> FileInfo | None:
    if view is None:
        return None
    return FileInfo(
        file_id=view.file_id,
        file_path=view.file_path,
        track_title=view.track_title,
        file_status=view.file_status,
        source=view.source,
    )


def _proposal(view: ProposalView) -> ProposalInfo:
    return ProposalInfo(
        id=view.id,
        identity_id=view.identity_id,
        kind=view.kind.value,
        reason=view.reason,
        state=view.state.value,
        title=view.title,
        artist_name=view.artist_name,
        plays=view.plays,
        work_id=view.work_id,
        work_title=view.work_title,
        performance_id=view.performance_id,
        plays_now=_file(view.plays_now),
        would_play=_file(view.would_play),
        candidates=[
            CandidateInfo(
                performance_id=c.performance_id,
                credit_names=list(c.credit_names),
                would_play=_file(c.would_play),
            )
            for c in view.candidates
        ],
    )


@router.get("/credit-picks", response_model=CreditPicksPage)
def get_credit_picks(_token: Token, repos: Repos) -> CreditPicksPage:
    """Both lists, stale rows left out (S9)."""
    page = service.page(repos)
    return CreditPicksPage(
        credit=[_proposal(v) for v in page.credit],
        version_lines=[_proposal(v) for v in page.version_lines],
        total=page.total,
    )


@router.get("/credit-picks/count", response_model=CreditPicksCount)
def get_credit_picks_count(_token: Token, repos: Repos) -> CreditPicksCount:
    """Library Status's count (D7)."""
    return CreditPicksCount(total=service.count(repos.credit_picks))


@router.post("/credit-picks/apply", response_model=ApplyResult)
def apply_credit_picks(_token: Token, body: ApplyRequest, repos: Repos) -> ApplyResult:
    """Apply the rows Lance saw minus his skips; record his skips (S9, C64, E6)."""
    try:
        result = service.apply(body.apply, body.skip, repos)
    except (ProposalKindError, ProposalTwiceError) as refused:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(refused)) from refused
    return ApplyResult(applied=result.applied, skipped=result.skipped, stale=list(result.stale))


@router.put("/credit-picks/{proposal_id}/pick", response_model=PickResult)
def pick_credit_performance(
    proposal_id: UUID, _token: Token, body: PickRequest, repos: Repos
) -> PickResult:
    """A version line's or tie's pick, stored at once (S9, C65)."""
    try:
        service.pick(proposal_id, body.performance_id, repos)
    except ProposalNotFoundError as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from missing
    except (ProposalKindError, CandidateNotListedError) as refused:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(refused)) from refused
    except ProposalStaleError as stale:
        raise HTTPException(status.HTTP_409_CONFLICT, str(stale)) from stale
    return PickResult(performance_id=body.performance_id)

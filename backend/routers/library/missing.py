"""Missing Files: list, remap and delete library rows whose file is gone (spec C2)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Self
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, model_validator

from backend.dependencies import SyncRepos, get_current_token, get_reconciliation_repos
from backend.domain.library import (
    MissingFileChangedError,
    MissingFileEntry,
    MissingFileNotFoundError,
    MissingFilePage,
    MissingFileSelection,
    RemapTargetNotFoundError,
    RemapTargetNotPresentError,
    RemapTargetUngroupedError,
)
from backend.services.missing_file_reconciliation_service import (
    ReconciliationRepos,
    delete_missing_files,
    list_missing_files,
    remap_missing_file,
)

router = APIRouter()

Repos = SyncRepos
ReconRepos = Annotated[ReconciliationRepos, Depends(get_reconciliation_repos)]
Token = Annotated[str, Depends(get_current_token)]


class CandidateOut(BaseModel):
    id: UUID
    file_path: str


class MissingFileOut(BaseModel):
    id: UUID
    file_path: str
    artist_name: str | None
    track_title: str | None
    release_title: str | None
    missing_since: datetime | None
    work_id: str | None
    work_title: str | None
    match_count: int
    work_has_present_file: bool
    candidates: list[CandidateOut]

    @classmethod
    def of(cls, entry: MissingFileEntry) -> Self:
        r = entry.row
        return cls(
            id=r.id,
            file_path=r.file_path,
            artist_name=r.artist_name,
            track_title=r.track_title,
            release_title=r.release_title,
            missing_since=r.missing_since,
            work_id=r.work_id,
            work_title=r.work_title,
            match_count=r.match_count,
            work_has_present_file=r.work_has_present_file,
            candidates=[CandidateOut(id=c.id, file_path=c.file_path) for c in entry.candidates],
        )


class MissingFilePageOut(BaseModel):
    items: list[MissingFileOut]
    total: int
    total_match_count: int

    @classmethod
    def of(cls, page: MissingFilePage) -> Self:
        return cls(
            items=[MissingFileOut.of(e) for e in page.entries],
            total=page.total,
            total_match_count=page.total_match_count,
        )


class RemapIn(BaseModel):
    target_file_id: UUID


class DeleteIn(BaseModel):
    ids: list[UUID] | None = None
    all: bool = False

    @model_validator(mode="after")
    def _ids_or_all(self) -> Self:
        if self.all == bool(self.ids):
            raise ValueError("send either ids or all: true")
        return self

    def selection(self) -> MissingFileSelection:
        if self.all:
            return MissingFileSelection(every_row=True)
        return MissingFileSelection(ids=tuple(self.ids or ()))


class DeletionOut(BaseModel):
    deleted: int
    matches_released: int
    skipped: int


@router.get("/missing-files", response_model=MissingFilePageOut)
def get_missing_files(
    repos: Repos,
    _token: Token,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> MissingFilePageOut:
    """A page of missing rows with their work, matches and candidate successors."""
    page = list_missing_files(offset, limit, repos.missing_files, repos.library_files)
    return MissingFilePageOut.of(page)


@router.post("/missing-files/{file_id}/remap", status_code=status.HTTP_204_NO_CONTENT)
def remap_file(file_id: UUID, body: RemapIn, recon: ReconRepos, _token: Token) -> None:
    """Fold a missing row into the chosen present file."""
    try:
        remap_missing_file(file_id, body.target_file_id, recon)
    except (MissingFileNotFoundError, RemapTargetNotFoundError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (RemapTargetNotPresentError, RemapTargetUngroupedError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.delete("/missing-files", response_model=DeletionOut)
def delete_files(body: DeleteIn, recon: ReconRepos, repos: Repos, _token: Token) -> DeletionOut:
    """Delete missing rows; their matches go back to review."""
    try:
        result = delete_missing_files(body.selection(), recon, repos.broadcast_identities)
    except MissingFileChangedError as exc:
        # A scan restored a row mid-delete; the dependency rolls the whole request back.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return DeletionOut(
        deleted=result.deleted,
        matches_released=result.matches_released,
        skipped=result.skipped,
    )

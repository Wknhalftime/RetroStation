"""Unmapped versions: the Library page of every unmapped version text, and its Dismiss and
Dismiss all (versions phase 2b PR 3, SPEC S7, S8, D5; design C62; AUD-R034)."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from pydantic import BaseModel, ConfigDict, Field

from backend.db.repositories.version_entries import AsyncVersionEntryReader
from backend.dependencies import get_current_token, get_db_connection, get_version_entry_repos
from backend.domain.curation import (
    CurationWorkNotFoundError,
    VersionArtistNotFoundError,
    VersionTextMappedError,
    VersionTextNotListedError,
)
from backend.routers.library.works import VersionSuggestionInfo, version_info
from backend.services import version_entry_service as service
from backend.services.version_entry_service import DismissSelection, VersionEntryRepos

router = APIRouter()

Token = Annotated[str, Depends(get_current_token)]
DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
VersionRepos = Annotated[VersionEntryRepos, Depends(get_version_entry_repos)]


class UnmappedVersionInfo(BaseModel):
    version_text: str
    version_type: str
    reads_as_version: bool
    plays: int
    stations: list[str]
    first_seen: date | None
    first_station: str | None
    new: bool
    suggestion: VersionSuggestionInfo | None
    work_id: str
    work_title: str
    artist_id: str
    artist_name: str


class UnmappedVersionPage(BaseModel):
    items: list[UnmappedVersionInfo]
    total: int
    new_total: int


class EntryKey(BaseModel):
    work_id: str
    version_text: str


class DismissRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    entries: list[EntryKey] | None = None
    every_listed: bool = Field(default=False, alias="all")
    artist_id: str | None = None


class DismissResult(BaseModel):
    dismissed: int


@router.get("/versions", response_model=UnmappedVersionPage)
async def get_unmapped_versions(
    conn: DbConn,
    _token: Token,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
) -> UnmappedVersionPage:
    """Every unmapped version text, by plays (S7)."""
    page = await service.unmapped_page(AsyncVersionEntryReader(conn), offset, limit)
    return UnmappedVersionPage(
        items=[
            UnmappedVersionInfo(
                **version_info(item.version).model_dump(exclude={"state", "mapped_file_id"}),
                work_id=item.version.work_id,
                work_title=item.work_title,
                artist_id=item.artist_id,
                artist_name=item.artist_name,
            )
            for item in page.items
        ],
        total=page.total,
        new_total=page.new_total,
    )


@router.post("/versions/dismiss", response_model=DismissResult)
def dismiss_unmapped_versions(
    _token: Token, body: DismissRequest, repos: VersionRepos
) -> DismissResult:
    """Dismiss selected, Dismiss all, or an artist page's Dismiss all (S7, D5)."""
    try:
        selection = DismissSelection(
            keys=None
            if body.entries is None
            else tuple((e.work_id, e.version_text) for e in body.entries),
            every_listed=body.every_listed,
            artist_id=body.artist_id,
        )
        return DismissResult(dismissed=service.dismiss_listed(selection, repos))
    except (VersionArtistNotFoundError, CurationWorkNotFoundError) as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from missing
    except VersionTextMappedError as mapped:
        raise HTTPException(status.HTTP_409_CONFLICT, str(mapped)) from mapped
    except VersionTextNotListedError as unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(unknown)) from unknown

"""Silent Files: the Library report of silent and nearly silent files (D111, S6; issue #146).

Read-only. Silent files are skipped on air (D111); nearly silent ones still play their few
audible seconds (S2). Both are listed so they can be found and replaced.
"""

from __future__ import annotations

from typing import Annotated, Self
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from backend.dependencies import SyncRepos, get_current_token
from backend.domain.streaming import SilenceReason, SilentFileEntry, SilentFileReport
from backend.domain.system import StorageUnavailableError
from backend.services.streaming.silent_files import silent_file_report

router = APIRouter()

# Declared first in every endpoint: FastAPI resolves dependencies in declaration order, so a
# request without a token gets its 401 before a connection is opened.
Token = Annotated[str, Depends(get_current_token)]


class SilentFileOut(BaseModel):
    file_id: UUID
    artist: str | None
    title: str | None
    album: str | None
    path: str
    duration_ms: int | None
    reasons: list[SilenceReason]
    bitrate_kbps: int | None
    cued_span_ms: int | None

    @classmethod
    def of(cls, entry: SilentFileEntry) -> Self:
        return cls(
            file_id=entry.file_id,
            artist=entry.artist,
            title=entry.title,
            album=entry.album,
            path=entry.path,
            duration_ms=entry.duration_ms,
            reasons=list(entry.reasons()),
            bitrate_kbps=entry.bitrate_kbps(),
            cued_span_ms=entry.cued_span_ms,
        )


class SilentFileReportOut(BaseModel):
    files: list[SilentFileOut]
    silent: int
    nearly_silent: int

    @classmethod
    def of(cls, report: SilentFileReport) -> Self:
        return cls(
            files=[SilentFileOut.of(entry) for entry in report.files],
            silent=report.silent,
            nearly_silent=report.nearly_silent,
        )


@router.get("/silent-files", response_model=SilentFileReportOut)
def get_silent_files(_token: Token, repos: SyncRepos) -> SilentFileReportOut:
    """Every present file whose audio is silent or nearly silent, silent first."""
    try:
        report = silent_file_report(repos.streaming.silent_files)
    except StorageUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable") from exc
    return SilentFileReportOut.of(report)

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.dependencies import (
    SyncRepos,
    get_current_token,
    get_master_choice_repos,
    get_version_entry_repos,
    get_work_curation_repos,
    get_work_merge_repos,
    get_work_page_repos,
)
from backend.domain.curation import (
    CopyNotInPerformanceError,
    CurationFileMissingError,
    CurationFileNotFoundError,
    CurationFileNotOnWorkError,
    CurationWorkNotFoundError,
    DefaultPerformanceCopyError,
    FileAlreadyOnWorkError,
    FormatOverrideExistsError,
    FormatOverrideNotFoundError,
    MasterFileMissingError,
    MasterFileNotOnWorkError,
    MasterWorkNotFoundError,
    MergeTargetInSourcesError,
    NewFormatOverride,
    PerformanceNotOnWorkError,
    PinNotOnWorkError,
    PreferredCopyMissingError,
    SongMasterNotFoundError,
    VersionArtistNotFoundError,
    VersionTextMappedError,
    VersionTextNotListedError,
    WorkCurationError,
)
from backend.domain.enums import FileStatus
from backend.domain.version_pages import MasterFlag, WorkVersion
from backend.domain.work_merges import (
    MergeApproval,
    MergeReason,
    MergeRefused,
    WorkMergeFailedError,
    WorkMergeGroup,
)
from backend.domain.work_page import CreditName, PageFile, PerformanceView, WorkDetailPage
from backend.services import version_entry_service as versions_service
from backend.services import work_curation_service as curation
from backend.services import work_merge_service as merges
from backend.services import work_page_service as pages
from backend.services.master_choice_service import MasterChoiceRepos, choose_master, prefer_copy
from backend.services.version_entry_service import VersionEntryRepos
from backend.services.work_curation_service import WorkCurationRepos
from backend.services.work_merge_service import WorkMergeRepos
from backend.services.work_page_service import WorkPageRepos

router = APIRouter()

# The read routes' readers, on the request's async connection; the writes run on SyncRepos.
PageRepos = Annotated[WorkPageRepos, Depends(get_work_page_repos)]
# Declared first in every write route: FastAPI resolves dependencies in declaration order, so
# a request without a token gets its 401 before a connection is opened.
Token = Annotated[str, Depends(get_current_token)]
ChoiceRepos = Annotated[MasterChoiceRepos, Depends(get_master_choice_repos)]
CurationRepos = Annotated[WorkCurationRepos, Depends(get_work_curation_repos)]
MergeRepos = Annotated[WorkMergeRepos, Depends(get_work_merge_repos)]
VersionRepos = Annotated[VersionEntryRepos, Depends(get_version_entry_repos)]


# ---------------------------------------------------------------------------
# Schemas — work detail
# ---------------------------------------------------------------------------


class OtherWorkRef(BaseModel):
    """The work a file is on, when a work's page shows it through a shared recording."""

    id: str
    title: str
    artists: list[str] = []


class FileInfo(BaseModel):
    id: UUID
    file_path: str
    format: str
    bitrate: int | None
    duration_ms: int | None
    track_title: str | None
    release_title: str | None
    enrichment_status: str
    file_status: FileStatus
    other_work: OtherWorkRef | None = None


class RecordingDetail(BaseModel):
    id: str
    title: str
    version_type: str
    duration_ms: int | None
    files: list[FileInfo]


class SongMasterInfo(BaseModel):
    id: UUID
    preferred_file_id: UUID
    selection_method: str
    score: int | None
    updated_at: datetime


class FormatOverrideInfo(BaseModel):
    id: UUID
    format_name: str
    preferred_file_id: UUID
    notes: str | None
    created_at: datetime


class VersionSuggestionInfo(BaseModel):
    file_id: UUID
    reason: str
    track_title: str | None
    file_path: str


class WorkVersionInfo(BaseModel):
    """One version text of the work's matched lines or entries (phase 2b S5)."""

    version_text: str
    version_type: str
    reads_as_version: bool
    state: str
    mapped_file_id: UUID | None
    plays: int
    stations: list[str]
    first_seen: date | None
    first_station: str | None
    new: bool
    suggestion: VersionSuggestionInfo | None


class MasterFlagInfo(BaseModel):
    master_file_id: UUID
    version_type: str
    plain_file_ids: list[UUID]


class WorkVersions(BaseModel):
    versions: list[WorkVersionInfo]
    master_flag: MasterFlagInfo | None


class WorkDetail(BaseModel):
    id: str
    title: str
    recordings: list[RecordingDetail]
    song_master: SongMasterInfo | None
    format_overrides: list[FormatOverrideInfo]
    versions: list[WorkVersionInfo] = []
    master_flag: MasterFlagInfo | None = None
    # 3b PR 5a (S8, D41 a, f, g); their schemas follow, so the model is rebuilt at the end.
    artists: list[CreditedArtistInfo] = []
    performances: list[PerformanceInfo] = []
    acts: list[ActInfo] = []


class MapVersionsRequest(BaseModel):
    version_texts: list[str]
    file_id: UUID


class VersionTextsRequest(BaseModel):
    version_texts: list[str]


# ---------------------------------------------------------------------------
# Schemas — master management
# ---------------------------------------------------------------------------


class SetMasterRequest(BaseModel):
    preferred_file_id: UUID


class SongMasterResponse(BaseModel):
    id: UUID
    work_id: str
    preferred_file_id: UUID
    selection_method: str
    score: int | None
    updated_at: datetime


# ---------------------------------------------------------------------------
# Schemas — format overrides management
# ---------------------------------------------------------------------------


class CreateFormatOverrideRequest(BaseModel):
    format_name: str
    preferred_file_id: UUID
    notes: str | None = None


class FormatOverrideResponse(BaseModel):
    id: UUID
    work_id: str
    format_name: str
    preferred_file_id: UUID
    notes: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Schemas — merge / split / reassign
# ---------------------------------------------------------------------------


class MergeRequest(BaseModel):
    source_work_ids: list[str]


class ApprovalBody(BaseModel):
    station_id: UUID
    identity_id: UUID
    file_id: UUID | None


class ApprovedMergeRequest(BaseModel):
    source_work_ids: list[str]
    approvals: list[ApprovalBody]


class MergeResponse(BaseModel):
    merged_file_count: int
    deleted_work_count: int
    dropped_override_count: int


class SplitRequest(BaseModel):
    file_id: UUID


class SplitResponse(BaseModel):
    new_work_id: str
    old_work_deleted: bool


class ReassignRequest(BaseModel):
    work_id: str


class ReassignResponse(BaseModel):
    old_work_id: str | None
    old_work_deleted: bool


# ---------------------------------------------------------------------------
# Schemas — the work page's performances (3b PR 5a; S8, D41)
# ---------------------------------------------------------------------------


class CreditedArtistInfo(BaseModel):
    id: str | None
    name: str


class PinnedLineInfo(BaseModel):
    identity_id: UUID
    station_id: UUID
    station: str
    title: str
    artist: str
    plays: int
    next_file_id: UUID | None
    next_path: str | None


class CopyInfo(BaseModel):
    file: FileInfo
    pins: list[PinnedLineInfo]


class PerformanceInfo(BaseModel):
    id: UUID
    credit: list[CreditedArtistInfo]
    credit_text: str
    version_type: str
    default: bool
    preferred_copy_id: UUID | None
    lines: int
    copies: list[CopyInfo]


class ActInfo(BaseModel):
    credit: str
    default: bool
    lines: int
    reached: dict[str, int]


class PreferCopyRequest(BaseModel):
    file_id: UUID


class ReleasePinsRequest(BaseModel):
    identity_ids: list[UUID] = Field(min_length=1)


class ReleasePinsResponse(BaseModel):
    released: int


# ---------------------------------------------------------------------------
# Mapping the work page service's read models
# ---------------------------------------------------------------------------


def _file_info(file: PageFile) -> FileInfo:
    other = file.other_work
    return FileInfo(
        id=file.id,
        file_path=file.file_path,
        format=file.format,
        bitrate=file.bitrate,
        duration_ms=file.duration_ms,
        track_title=file.track_title,
        release_title=file.release_title,
        enrichment_status=file.enrichment_status,
        file_status=file.file_status,
        other_work=None
        if other is None
        else OtherWorkRef(
            id=other.id,
            title=other.title,
            artists=list(other.artists),
        ),
    )


def _credited(parts: tuple[CreditName, ...]) -> list[CreditedArtistInfo]:
    return [CreditedArtistInfo(id=p.artist_id, name=p.name) for p in parts]


def _performance_info(
    performance: PerformanceView, copies: Mapping[UUID, PageFile]
) -> PerformanceInfo:
    return PerformanceInfo(
        id=performance.id,
        credit=_credited(performance.credit),
        credit_text=performance.credit_text,
        version_type=performance.version_type,
        default=performance.default,
        preferred_copy_id=performance.preferred_copy_id,
        lines=performance.lines,
        copies=[
            CopyInfo(
                file=_file_info(copies[c.file_id]),
                pins=[PinnedLineInfo(**asdict(pin)) for pin in c.pins],
            )
            for c in performance.copies
        ],
    )


def _master_flag(flag: MasterFlag | None) -> MasterFlagInfo | None:
    if flag is None:
        return None
    return MasterFlagInfo(
        master_file_id=flag.master_file_id,
        version_type=flag.version_type.value,
        plain_file_ids=list(flag.plain_file_ids),
    )


def _work_detail(detail: WorkDetailPage) -> WorkDetail:
    master = detail.song_master
    return WorkDetail(
        id=detail.id,
        title=detail.title,
        artists=_credited(detail.page.artists),
        recordings=[
            RecordingDetail(
                id=r.id,
                title=r.title,
                version_type=r.version_type,
                duration_ms=r.duration_ms,
                files=[_file_info(f) for f in r.files],
            )
            for r in detail.recordings
        ],
        song_master=None if master is None else SongMasterInfo(**asdict(master)),
        format_overrides=[FormatOverrideInfo(**asdict(o)) for o in detail.format_overrides],
        versions=[version_info(v) for v in detail.versions],
        master_flag=_master_flag(detail.master_flag),
        performances=[_performance_info(p, detail.copies) for p in detail.page.performances],
        acts=[ActInfo(**asdict(a)) for a in detail.page.acts],
    )


def version_info(version: WorkVersion) -> WorkVersionInfo:
    suggestion = version.suggestion
    return WorkVersionInfo(
        version_text=version.version_text,
        version_type=version.version_type.value,
        reads_as_version=version.reads_as_version,
        state=version.state.value,
        mapped_file_id=version.mapped_file_id,
        plays=version.plays,
        stations=list(version.stations),
        first_seen=version.first_seen,
        first_station=version.first_station,
        new=version.new,
        suggestion=None
        if suggestion is None
        else VersionSuggestionInfo(
            file_id=suggestion.file_id,
            reason=suggestion.reason.value,
            track_title=suggestion.track_title,
            file_path=suggestion.file_path,
        ),
    )


# ---------------------------------------------------------------------------
# Routes — reads, through the work page service (3b PR 5a)
# ---------------------------------------------------------------------------


@router.get("/works/{work_id}", response_model=WorkDetail)
async def get_work_detail(work_id: str, repos: PageRepos, _token: Token) -> WorkDetail:
    """Return a work with its performances, recordings, files, master and format overrides."""
    try:
        detail = await pages.work_detail(work_id, repos)
    except CurationWorkNotFoundError as missing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(missing)) from missing
    return _work_detail(detail)


@router.get("/works/{work_id}/versions", response_model=WorkVersions)
async def get_work_versions(work_id: str, repos: PageRepos, _token: Token) -> WorkVersions:
    """The version texts of the work's matched lines and entries, and the master flag (S5)."""
    try:
        versions, flag = await pages.work_versions(work_id, repos)
    except CurationWorkNotFoundError as missing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(missing)) from missing
    return WorkVersions(
        versions=[version_info(v) for v in versions], master_flag=_master_flag(flag)
    )


# ---------------------------------------------------------------------------
# Routes — writes: sync routes on the request's one connection (SyncRepos, C75), each
# calling work_curation_service (or master_choice_service); a refusal's detail is the
# service error's message
# ---------------------------------------------------------------------------

_REFUSAL_STATUS: dict[type[WorkCurationError], int] = {
    CurationWorkNotFoundError: status.HTTP_404_NOT_FOUND,
    CurationFileNotFoundError: status.HTTP_404_NOT_FOUND,
    FormatOverrideNotFoundError: status.HTTP_404_NOT_FOUND,
    SongMasterNotFoundError: status.HTTP_404_NOT_FOUND,
    FormatOverrideExistsError: status.HTTP_409_CONFLICT,
    CurationFileNotOnWorkError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    CurationFileMissingError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    FileAlreadyOnWorkError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    MergeTargetInSourcesError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    VersionTextNotListedError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    VersionTextMappedError: status.HTTP_409_CONFLICT,
    VersionArtistNotFoundError: status.HTTP_404_NOT_FOUND,
    PerformanceNotOnWorkError: status.HTTP_404_NOT_FOUND,
    DefaultPerformanceCopyError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    CopyNotInPerformanceError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    PreferredCopyMissingError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    PinNotOnWorkError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}
_REFUSALS = tuple(_REFUSAL_STATUS)


def _refused(error: WorkCurationError) -> HTTPException:
    """The refusal's status: its own class's, else the nearest mapped base class's."""
    code = next(_REFUSAL_STATUS[cls] for cls in type(error).__mro__ if cls in _REFUSAL_STATUS)
    return HTTPException(status_code=code, detail=str(error))


@router.put("/works/{work_id}/master", response_model=SongMasterResponse)
def set_work_master(
    _token: Token, work_id: str, body: SetMasterRequest, repos: ChoiceRepos
) -> SongMasterResponse:
    """Make a file the work's manual master (UPSERT).

    A file the work's page shows from another work (a shared recording) is moved onto
    this work first.
    """
    try:
        master = choose_master(work_id, body.preferred_file_id, repos)
    except MasterWorkNotFoundError as missing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(missing)) from missing
    except (MasterFileNotOnWorkError, MasterFileMissingError) as refused:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(refused)
        ) from refused
    return SongMasterResponse(
        id=master.id,
        work_id=master.work_id,
        preferred_file_id=master.preferred_file_id,
        selection_method=master.selection_method,
        score=master.score,
        updated_at=master.updated_at,
    )


@router.delete("/works/{work_id}/master", status_code=status.HTTP_204_NO_CONTENT)
def delete_work_master(_token: Token, work_id: str, repos: SyncRepos) -> None:
    """Remove the manual song master for a work."""
    try:
        curation.delete_master(work_id, repos.song_masters)
    except _REFUSALS as refused:
        raise _refused(refused) from refused


@router.post(
    "/works/{work_id}/format-overrides",
    response_model=FormatOverrideResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_format_override(
    _token: Token, work_id: str, body: CreateFormatOverrideRequest, repos: CurationRepos
) -> FormatOverrideResponse:
    """Create a per-format preferred file override for a work."""
    new = NewFormatOverride(
        format_name=body.format_name, preferred_file_id=body.preferred_file_id, notes=body.notes
    )
    try:
        stored = curation.create_format_override(work_id, new, repos)
    except _REFUSALS as refused:
        raise _refused(refused) from refused
    return FormatOverrideResponse(
        id=stored.id,
        work_id=stored.work_id,
        format_name=stored.format_name,
        preferred_file_id=stored.preferred_file_id,
        notes=stored.notes,
        created_at=stored.created_at,
    )


@router.delete(
    "/works/{work_id}/format-overrides/{override_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_format_override(
    _token: Token, work_id: str, override_id: UUID, repos: SyncRepos
) -> None:
    """Delete a format override by ID."""
    try:
        curation.delete_format_override(work_id, override_id, repos.format_overrides)
    except _REFUSALS as refused:
        raise _refused(refused) from refused


@router.post("/works/{work_id}/version-entries", status_code=status.HTTP_204_NO_CONTENT)
def map_work_versions(
    _token: Token, work_id: str, body: MapVersionsRequest, repos: VersionRepos
) -> None:
    """Map one or more version texts to a present file of the work (S5, E3, E5)."""
    try:
        versions_service.map_versions(work_id, body.version_texts, body.file_id, repos)
    except _REFUSALS as refused:
        raise _refused(refused) from refused


@router.delete("/works/{work_id}/version-entries", status_code=status.HTTP_204_NO_CONTENT)
def unmap_work_versions(
    _token: Token, work_id: str, body: VersionTextsRequest, repos: VersionRepos
) -> None:
    """Unmap version texts; their states stay (S5)."""
    try:
        versions_service.unmap_versions(work_id, body.version_texts, repos)
    except _REFUSALS as refused:
        raise _refused(refused) from refused


@router.post("/works/{work_id}/versions/dismiss", status_code=status.HTTP_204_NO_CONTENT)
def dismiss_work_versions(
    _token: Token, work_id: str, body: VersionTextsRequest, repos: VersionRepos
) -> None:
    """Dismiss version texts: the master is fine (S5)."""
    try:
        versions_service.dismiss_versions(work_id, body.version_texts, repos)
    except _REFUSALS as refused:
        raise _refused(refused) from refused


def _optional_id(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def _awaiting_approval(refused: MergeRefused) -> HTTPException:
    """409 with the air-play changes the merge waits on (3b D3, Q7), for the artist page."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": "This merge changes what some lines play; approve the changes.",
            "changes": [
                {
                    "station_id": str(c.station_id),
                    "station": c.station,
                    "identity_id": str(c.identity_id),
                    "title": c.title,
                    "artist": c.artist,
                    "plays": c.plays,
                    "from_file_id": _optional_id(c.from_file_id),
                    "from_path": c.from_path,
                    "to_file_id": _optional_id(c.to_file_id),
                    "to_path": c.to_path,
                }
                for c in refused.changes
            ],
        },
    )


def _guarded(
    target_id: str, source_ids: list[str], approvals: frozenset[MergeApproval], repos: MergeRepos
) -> MergeResponse:
    group = WorkMergeGroup(target_id, tuple(source_ids), MergeReason.LIBRARY, approvals)
    try:
        outcome = merges.guarded_merge(group, repos)
    except _REFUSALS as refused:
        raise _refused(refused) from refused
    except WorkMergeFailedError as failed:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(failed)) from failed
    if isinstance(outcome, MergeRefused):
        raise _awaiting_approval(outcome)
    return MergeResponse(
        merged_file_count=outcome.result.merged_file_count,
        deleted_work_count=outcome.result.deleted_work_count,
        dropped_override_count=outcome.result.dropped_override_count,
    )


@router.post("/works/{target_id}/merge", response_model=MergeResponse)
def merge_works(
    _token: Token, target_id: str, body: MergeRequest, repos: MergeRepos
) -> MergeResponse:
    """Merge one or more source works into a target work."""
    return _guarded(target_id, body.source_work_ids, frozenset(), repos)


@router.post("/works/{target_id}/merge/approved", response_model=MergeResponse)
def merge_works_approved(
    _token: Token, target_id: str, body: ApprovedMergeRequest, repos: MergeRepos
) -> MergeResponse:
    """Merge with the air-play changes Lance approved."""
    approvals = frozenset(
        MergeApproval(a.station_id, a.identity_id, a.file_id) for a in body.approvals
    )
    return _guarded(target_id, body.source_work_ids, approvals, repos)


@router.post(
    "/works/{work_id}/split",
    response_model=SplitResponse,
    status_code=status.HTTP_201_CREATED,
)
def split_work(
    _token: Token, work_id: str, body: SplitRequest, repos: CurationRepos
) -> SplitResponse:
    """Split a single file out of a work into its own new work."""
    try:
        split = curation.split_file_off(work_id, body.file_id, repos)
    except _REFUSALS as refused:
        raise _refused(refused) from refused
    return SplitResponse(new_work_id=split.new_work_id, old_work_deleted=split.old_work_deleted)


@router.patch("/files/{file_id}/work", response_model=ReassignResponse)
def reassign_file_work(
    _token: Token, file_id: UUID, body: ReassignRequest, repos: CurationRepos
) -> ReassignResponse:
    """Reassign a library file to a different work."""
    try:
        moved = curation.reassign_file(file_id, body.work_id, repos)
    except _REFUSALS as refused:
        raise _refused(refused) from refused
    return ReassignResponse(old_work_id=moved.old_work_id, old_work_deleted=moved.old_work_deleted)


@router.put(
    "/works/{work_id}/performances/{performance_id}/preferred-copy",
    status_code=status.HTTP_204_NO_CONTENT,
)
def prefer_work_copy(
    _token: Token, work_id: str, performance_id: UUID, body: PreferCopyRequest, repos: ChoiceRepos
) -> None:
    """Prefer this copy: the copy a stored performance's lines play (3b PR 5a, D41 c)."""
    try:
        prefer_copy(work_id, performance_id, body.file_id, repos)
    except _REFUSALS as refused:
        raise _refused(refused) from refused


@router.post("/works/{work_id}/pins/release", response_model=ReleasePinsResponse)
def release_work_pins(
    _token: Token, work_id: str, body: ReleasePinsRequest, repos: CurationRepos
) -> ReleasePinsResponse:
    """Release: the named lines leave their pins on the work's copies (3b PR 5a, D41 d, e)."""
    try:
        released = curation.release_line_pins(work_id, body.identity_ids, repos)
    except _REFUSALS as refused:
        raise _refused(refused) from refused
    return ReleasePinsResponse(released=released)


WorkDetail.model_rebuild()

"""The work page's reads: one work's detail and its version list (artist model phase 3b PR 5a;
SPEC S8, D41; the read routes left the router, D12 item 5).

A synthetic id (an artist's MBID and a title, for files on no work) answers its files as one
recording; a real id answers the work it names or was merged into, with its recordings,
master, overrides, versions and :func:`backend.domain.work_page.work_page`'s performances,
artists and acts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from backend.domain.curation import CurationWorkNotFoundError
from backend.domain.synthetic_work_id import decode as decode_synthetic_work_id
from backend.domain.version_pages import MasterFlag, WorkVersion
from backend.domain.work_page import (
    MasterCopy,
    PageFile,
    PageMaster,
    PageOverride,
    PageRecording,
    PerformanceFacts,
    WorkDetailPage,
    WorkHead,
    WorkLine,
    work_page,
)
from backend.services import version_entry_service as versions_service
from backend.services.version_entry_service import VersionPageReader


class WorkPageReader(Protocol):
    async def work(self, work_id: str) -> WorkHead | None: ...

    async def exists(self, work_id: str) -> bool: ...

    async def recordings(self, work_id: str, title: str) -> list[PageRecording]: ...

    async def song_master(self, work_id: str) -> PageMaster | None: ...

    async def format_overrides(self, work_id: str) -> list[PageOverride]: ...

    async def performances(
        self, work_id: str
    ) -> tuple[list[PerformanceFacts], dict[UUID, PageFile]]: ...

    async def lines(self, work_id: str) -> list[WorkLine]: ...

    async def synthetic_files(self, artist_mbid: str, title: str) -> list[PageFile]: ...


@dataclass(frozen=True)
class WorkPageRepos:
    """The work page's readers, on the request's async connection."""

    pages: WorkPageReader
    versions: VersionPageReader


def _not_found(work_id: str) -> CurationWorkNotFoundError:
    return CurationWorkNotFoundError(f"Work {work_id} not found")


async def work_detail(work_id: str, repos: WorkPageRepos) -> WorkDetailPage:
    """The work page's detail of *work_id*; raises when there is no such work."""
    synthetic = decode_synthetic_work_id(work_id)
    if synthetic is not None:
        return await _synthetic_detail(work_id, *synthetic, repos)
    head = await repos.pages.work(work_id)
    if head is None:
        raise _not_found(work_id)
    recordings = await repos.pages.recordings(head.id, head.title)
    master = await repos.pages.song_master(head.id)
    versions, flag = await versions_service.work_versions(repos.versions, head.id)
    performances, copies = await repos.pages.performances(head.id)
    lines = await repos.pages.lines(head.id)
    return WorkDetailPage(
        id=head.id,
        title=head.title,
        artist_id=head.artist_id,
        recordings=tuple(recordings),
        song_master=master,
        format_overrides=tuple(await repos.pages.format_overrides(head.id)),
        versions=tuple(versions),
        master_flag=flag,
        page=work_page(performances, _master_copy(master, recordings, copies), lines),
        copies=copies,
    )


def _master_copy(
    master: PageMaster | None, recordings: list[PageRecording], copies: dict[UUID, PageFile]
) -> MasterCopy | None:
    if master is None:
        return None
    paths = {f.id: f.file_path for r in recordings for f in r.files}
    paths |= {file_id: f.file_path for file_id, f in copies.items()}
    return MasterCopy(master.preferred_file_id, paths.get(master.preferred_file_id, ""))


async def _synthetic_detail(
    work_id: str, artist_mbid: str, title: str, repos: WorkPageRepos
) -> WorkDetailPage:
    files = await repos.pages.synthetic_files(artist_mbid, title)
    if not files:
        raise _not_found(work_id)
    recording = PageRecording(work_id, title, "original", files[0].duration_ms, tuple(files))
    return WorkDetailPage(
        id=work_id,
        title=title,
        artist_id=artist_mbid,
        recordings=(recording,),
        song_master=None,
        format_overrides=(),
    )


async def work_versions(
    work_id: str, repos: WorkPageRepos
) -> tuple[list[WorkVersion], MasterFlag | None]:
    """The work's version list and master flag (2b S5); none for a synthetic id."""
    if decode_synthetic_work_id(work_id) is not None:
        return [], None
    if not await repos.pages.exists(work_id):
        raise _not_found(work_id)
    return await versions_service.work_versions(repos.versions, work_id)

"""Version entries on the work page and the Unmapped versions page: Map, Unmap, Dismiss and
Dismiss all, and the reads that list them (versions phase 2b PR 3, SPEC S5-S8, S11, S12; design
M30, C62, C64, C66; AUD-R034, AUD-R046).

Nothing here maps a suggestion: only Map, on Lance's press, writes an entry (C64).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from backend.domain.curation import (
    CurationFileMissingError,
    CurationFileNotOnWorkError,
    CurationWorkNotFoundError,
    VersionArtistNotFoundError,
    VersionTextMappedError,
    VersionTextNotListedError,
)
from backend.domain.enums import FileStatus
from backend.domain.version_pages import (
    MasterFlag,
    VersionEntryRow,
    VersionState,
    WorkFile,
    WorkVersion,
    master_flag,
    work_page_order,
    work_version,
)
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.version_entries import VersionEntryRepository
from backend.repositories.works import WorkRepository


@dataclass(frozen=True)
class VersionEntryRepos:
    works: WorkRepository
    files: LibraryFileRepository
    versions: VersionEntryRepository


@dataclass(frozen=True)
class DismissSelection:
    """What Dismiss on the Unmapped versions page names: these (work, text) keys, every listed
    one, or every listed one of an artist's group. Exactly one."""

    keys: tuple[tuple[str, str], ...] | None = None
    every_listed: bool = False
    artist_id: str | None = None

    def __post_init__(self) -> None:
        given = [self.keys is not None, self.every_listed, self.artist_id is not None]
        if sum(given) != 1:
            raise VersionTextNotListedError("name exactly one of entries, all or artist_id")


@dataclass(frozen=True)
class UnmappedItem:
    version: WorkVersion
    work_title: str
    artist_id: str
    artist_name: str


@dataclass(frozen=True)
class UnmappedPage:
    items: list[UnmappedItem]
    total: int
    new_total: int


def _versions(
    rows: list[VersionEntryRow],
    line_files: dict[tuple[str, str], frozenset[UUID]],
    files: dict[str, list[WorkFile]],
) -> list[WorkVersion]:
    return [
        work_version(
            row,
            files.get(row.work_id, []),
            line_files.get((row.work_id, row.version_text), frozenset()),
        )
        for row in rows
    ]


def _listed(version: WorkVersion) -> bool:
    """On the Unmapped versions page: unmapped, and reads as a version."""
    return version.state is VersionState.UNMAPPED and version.reads_as_version


# --- writes (sync) ----------------------------------------------------------------------------


def _work_rows(work_id: str, repos: VersionEntryRepos) -> dict[str, VersionEntryRow]:
    if repos.works.get_by_id(work_id) is None:
        raise CurationWorkNotFoundError(f"Work {work_id} not found")
    return {row.version_text: row for row in repos.versions.entry_rows([work_id])}


def _require_listed(texts: list[str], listed: Iterable[str], work_id: str) -> None:
    known = set(listed)
    unknown = [text for text in texts if text not in known]
    if not texts or unknown:
        raise VersionTextNotListedError(
            f"Work {work_id} lists no version text {unknown!r}" if unknown else "no texts given"
        )


def map_versions(work_id: str, texts: list[str], file_id: UUID, repos: VersionEntryRepos) -> None:
    """Map each text to a present file of the work (the format overrides' rule, E3); seen now."""
    rows = _work_rows(work_id, repos)
    file = repos.files.get_by_id(file_id)
    if file is None or file.work_id != work_id:
        raise CurationFileNotOnWorkError(f"File {file_id} does not belong to work {work_id}")
    if file.file_status != FileStatus.PRESENT:
        raise CurationFileMissingError(f"File {file_id} is missing from disk")
    _require_listed(texts, rows, work_id)
    repos.versions.map_texts(work_id, list(dict.fromkeys(texts)), file_id)


def unmap_versions(work_id: str, texts: list[str], repos: VersionEntryRepos) -> None:
    if repos.works.get_by_id(work_id) is None:
        raise CurationWorkNotFoundError(f"Work {work_id} not found")
    repos.versions.unmap_texts(work_id, texts)


def dismiss_versions(work_id: str, texts: list[str], repos: VersionEntryRepos) -> None:
    rows = _work_rows(work_id, repos)
    _require_listed(texts, rows, work_id)
    _refuse_mapped([rows[text] for text in texts])
    repos.versions.dismiss([(work_id, text) for text in dict.fromkeys(texts)])


def _refuse_mapped(rows: list[VersionEntryRow]) -> None:
    mapped = [row.version_text for row in rows if row.mapped_file_id is not None]
    if mapped:
        raise VersionTextMappedError(f"Version {mapped[0]!r} is mapped; unmap it first")


def dismiss_listed(selection: DismissSelection, repos: VersionEntryRepos) -> int:
    """Dismiss selected, Dismiss all, or an artist page's Dismiss all; how many it dismissed."""
    if selection.keys is not None:
        return _dismiss_keys(list(dict.fromkeys(selection.keys)), repos)
    work_ids: list[str] | None = None
    if selection.artist_id is not None:
        work_ids = repos.versions.group_work_ids(selection.artist_id)
        if work_ids is None:
            raise VersionArtistNotFoundError(f"Artist {selection.artist_id} not found")
    rows = repos.versions.entry_rows(work_ids)
    works = sorted({row.work_id for row in rows})
    versions = _versions(
        rows, repos.versions.line_files(work_ids), repos.versions.work_files(works)
    )
    # The page's Dismiss all takes every listed one; an artist page's, the new ones it counts
    # (question D).
    keys = [
        (v.work_id, v.version_text)
        for v in versions
        if _listed(v) and (selection.every_listed or v.new)
    ]
    repos.versions.dismiss(keys)
    return len(keys)


def _dismiss_keys(keys: list[tuple[str, str]], repos: VersionEntryRepos) -> int:
    works = sorted({work for work, _ in keys})
    rows = {(row.work_id, row.version_text): row for row in repos.versions.entry_rows(works)}
    unknown = [key for key in keys if key not in rows]
    if not keys or unknown:
        raise VersionTextNotListedError(f"No such version entries: {unknown!r}")
    _refuse_mapped([rows[key] for key in keys])
    repos.versions.dismiss(keys)
    return len(keys)


# --- reads (async, the read routes' connection) -------------------------------------------------


class VersionPageReader(Protocol):
    async def entry_rows(self, work_ids: list[str] | None) -> list[VersionEntryRow]: ...

    async def line_files(
        self, work_ids: list[str] | None
    ) -> dict[tuple[str, str], frozenset[UUID]]: ...

    async def work_files(self, work_ids: list[str]) -> dict[str, list[WorkFile]]: ...

    async def master(self, work_id: str) -> tuple[UUID | None, str | None]: ...

    async def work_titles(self, work_ids: list[str]) -> dict[str, dict[str, object]]: ...


async def _read_versions(
    reader: VersionPageReader, work_ids: list[str] | None
) -> tuple[list[WorkVersion], dict[str, list[WorkFile]]]:
    rows = await reader.entry_rows(work_ids)
    works = sorted({row.work_id for row in rows} | set(work_ids or []))
    files = await reader.work_files(works)
    return _versions(rows, await reader.line_files(work_ids), files), files


async def work_versions(
    reader: VersionPageReader, work_id: str
) -> tuple[list[WorkVersion], MasterFlag | None]:
    """S5, S12: the work page's list, and its master flag."""
    versions, files = await _read_versions(reader, [work_id])
    master_id, master_title = await reader.master(work_id)
    return work_page_order(versions), master_flag(master_id, master_title, files.get(work_id, []))


async def unmapped_page(reader: VersionPageReader, offset: int, limit: int) -> UnmappedPage:
    """S7: every unmapped version over all works, by plays, then work title and text."""
    versions, _ = await _read_versions(reader, None)
    listed = [v for v in versions if _listed(v)]
    titles = await reader.work_titles(sorted({v.work_id for v in listed}))
    items = [
        UnmappedItem(
            version=v,
            work_title=str(titles[v.work_id]["title"]),
            artist_id=str(titles[v.work_id]["artist_id"]),
            artist_name=str(titles[v.work_id]["artist_name"]),
        )
        for v in listed
        if v.work_id in titles
    ]
    items.sort(key=lambda i: (-i.version.plays, i.work_title, i.version.version_text))
    return UnmappedPage(
        items=items[offset : offset + limit],
        total=len(items),
        new_total=sum(1 for i in items if i.version.new),
    )


async def new_counts(reader: VersionPageReader, work_ids: list[str]) -> dict[str, int]:
    """D5: how many new versions each work has."""
    if not work_ids:
        return {}
    versions, _ = await _read_versions(reader, work_ids)
    counts: dict[str, int] = {}
    for version in versions:
        if version.new:
            counts[version.work_id] = counts.get(version.work_id, 0) + 1
    return counts

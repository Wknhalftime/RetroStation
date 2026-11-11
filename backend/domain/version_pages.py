"""The Library's version pages: what each version text on a work reads as, its state, the
new-version marker, suggestions and the master flag (versions phase 2b PR 3, SPEC S5-S8, S11,
S12; design C62, C64, C66; AUD-R034, AUD-R046).

Pure functions over rows a repository reads; nothing here writes, so nothing here is ever
applied (C64).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from backend.domain.enums import VersionType
from backend.domain.log_version_reader import read_log_version

# A text read as title words or as the original never reads as a version (the PR 3 lock
# request's question C): it is listed below the versions and never warns.
_NOT_VERSIONS = frozenset({VersionType.UNKNOWN, VersionType.ORIGINAL})
# C66: a master whose title reads one of these is flagged when the work holds a plain copy.
_FLAGGED_MASTER = frozenset(
    {
        VersionType.LIVE,
        VersionType.ACOUSTIC,
        VersionType.DEMO,
        VersionType.REMIX,
        VersionType.EXTENDED,
    }
)
# A plain copy's title has no version text, or one that reads as title words, the original or a
# remaster.
_PLAIN = frozenset({VersionType.UNKNOWN, VersionType.ORIGINAL, VersionType.REMASTER})
# C59's rule, as the generated track_identities.version_text applies it: the title's trailing
# round or square bracket groups.
_TRAILING_GROUPS = re.compile(r"((?:\s*(?:\([^()\[\]]*\)|\[[^()\[\]]*\]))+)\s*$")
_BRACKETS = re.compile(r"[()\[\]]")


class VersionState(StrEnum):
    UNMAPPED = "unmapped"
    MAPPED = "mapped"
    DISMISSED = "dismissed"


class SuggestionReason(StrEnum):
    MATCHED_FILE = "matched_file"
    ONLY_COPY = "only_copy"


@dataclass(frozen=True)
class VersionEntryRow:
    """One (work, version text) as read: from the work's matched lines, its version entry and
    its state row."""

    work_id: str
    version_text: str
    plays: int
    stations: tuple[str, ...]
    first_seen: date | None
    first_station: str | None
    mapped_file_id: UUID | None
    dismissed: bool
    seen_at: datetime | None  # None: no state row


@dataclass(frozen=True)
class WorkFile:
    """A present file whose own work_id is the work."""

    work_id: str
    file_id: UUID
    version_type: str | None  # its recording's
    indexed_at: datetime
    track_title: str | None
    file_path: str


@dataclass(frozen=True)
class VersionSuggestion:
    file_id: UUID
    reason: SuggestionReason
    track_title: str | None
    file_path: str


@dataclass(frozen=True)
class WorkVersion:
    work_id: str
    version_text: str
    version_type: VersionType
    reads_as_version: bool
    state: VersionState
    mapped_file_id: UUID | None
    plays: int
    stations: tuple[str, ...]
    first_seen: date | None
    first_station: str | None
    new: bool
    suggestion: VersionSuggestion | None


@dataclass(frozen=True)
class MasterFlag:
    master_file_id: UUID
    version_type: VersionType
    plain_file_ids: tuple[UUID, ...]


def reads_as_version(version_type: VersionType) -> bool:
    return version_type not in _NOT_VERSIONS


def title_version_text(title: str | None) -> str | None:
    """A title's version text by C59's rule; None when no bracket group ends it."""
    if title is None:
        return None
    trailing = _TRAILING_GROUPS.search(title)
    if trailing is None:
        return None
    text = re.sub(r"\s+", " ", _BRACKETS.sub(" ", trailing.group(1)).lower()).strip()
    return text or None


def _state(row: VersionEntryRow) -> VersionState:
    if row.mapped_file_id is not None:
        return VersionState.MAPPED
    if row.dismissed:
        return VersionState.DISMISSED
    return VersionState.UNMAPPED


def _copies(files: list[WorkFile], version_type: VersionType) -> list[WorkFile]:
    return [f for f in files if f.version_type == version_type.value]


def _is_new(row: VersionEntryRow, state: VersionState, copies: list[WorkFile]) -> bool:
    """S8, D2: no state row, or a copy indexed after the row's seen_at ("may answer")."""
    if state is not VersionState.UNMAPPED:
        return False
    if row.seen_at is None:
        return True
    return any(copy.indexed_at > row.seen_at for copy in copies)


def _suggestion(copies: list[WorkFile], line_files: frozenset[UUID]) -> VersionSuggestion | None:
    """S11, C64: the one copy the text's lines are matched to, else the work's one copy."""
    matched = [copy for copy in copies if copy.file_id in line_files]
    for picks, reason in (
        (matched, SuggestionReason.MATCHED_FILE),
        (copies, SuggestionReason.ONLY_COPY),
    ):
        if len(picks) == 1:
            pick = picks[0]
            return VersionSuggestion(pick.file_id, reason, pick.track_title, pick.file_path)
    return None


def version_is_new(row: VersionEntryRow, files: list[WorkFile]) -> bool:
    """S8, D2: the new-version marker of one text; *files* are the work's present files."""
    version_type = read_log_version(row.version_text)
    if not reads_as_version(version_type):
        return False
    return _is_new(row, _state(row), _copies(files, version_type))


def work_version(
    row: VersionEntryRow, files: list[WorkFile], line_files: frozenset[UUID]
) -> WorkVersion:
    """One text of a work as its pages show it; *files* are the work's present files, and
    *line_files* the best-match files of the text's lines."""
    version_type = read_log_version(row.version_text)
    is_version = reads_as_version(version_type)
    state = _state(row)
    copies = _copies(files, version_type) if is_version else []
    return WorkVersion(
        work_id=row.work_id,
        version_text=row.version_text,
        version_type=version_type,
        reads_as_version=is_version,
        state=state,
        mapped_file_id=row.mapped_file_id,
        plays=row.plays,
        stations=row.stations,
        first_seen=row.first_seen,
        first_station=row.first_station,
        new=version_is_new(row, files),
        suggestion=(_suggestion(copies, line_files) if state is VersionState.UNMAPPED else None),
    )


def work_page_order(versions: list[WorkVersion]) -> list[WorkVersion]:
    """Versions first, then the other texts; each by plays, most first, then by text."""
    return sorted(versions, key=lambda v: (not v.reads_as_version, -v.plays, v.version_text))


def master_flag(
    master_file_id: UUID | None, master_title: str | None, files: list[WorkFile]
) -> MasterFlag | None:
    """S12, C66: the master's title reads live, acoustic, demo, remix or extended, and the work
    holds a plain copy. A flag only."""
    if master_file_id is None:
        return None
    text = title_version_text(master_title)
    if text is None:
        return None
    reading = read_log_version(text)
    if reading not in _FLAGGED_MASTER:
        return None
    plain = sorted(
        (f.file_id for f in files if f.file_id != master_file_id and _is_plain(f.track_title)),
        key=str,
    )
    if not plain:
        return None
    return MasterFlag(master_file_id, reading, tuple(plain))


def _is_plain(title: str | None) -> bool:
    text = title_version_text(title)
    return text is None or read_log_version(text) in _PLAIN

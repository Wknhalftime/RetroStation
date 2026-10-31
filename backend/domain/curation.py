from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from backend.domain.enums import FileStatus, SelectionMethod
from backend.domain.system import StorageUnavailableError


class CurationError(Exception):
    """Base class for curation-subdomain (song masters, format overrides) errors."""


class CurationStorageError(CurationError, StorageUnavailableError):
    """A curation repository lost its database connection mid-operation."""


class MasterChoiceError(CurationError):
    """A file cannot be made a work's master by hand."""


class MasterWorkNotFoundError(MasterChoiceError):
    """The work to choose a master for does not exist."""


class MasterFileNotOnWorkError(MasterChoiceError):
    """The file is neither on the work nor shown on its page through a shared recording."""


class MasterFileMissingError(MasterChoiceError):
    """The file is no longer on disk, so it cannot be a master."""


class WorkCurationError(CurationError):
    """A works write (merge, split, reassign, format override, master delete) was refused.

    Each message is the detail the works routes answered with before phase 0c moved the
    writes into ``work_curation_service``.
    """


class CurationWorkNotFoundError(WorkCurationError):
    """The work the write names does not exist."""


class CurationFileNotFoundError(WorkCurationError):
    """The file the write names does not exist."""


class CurationFileNotOnWorkError(WorkCurationError):
    """The file is not on the work the write names."""


class CurationFileMissingError(WorkCurationError):
    """The file is no longer on disk."""


class FileAlreadyOnWorkError(WorkCurationError):
    """The file is already on the work it would be reassigned to."""


class MergeTargetInSourcesError(WorkCurationError):
    """A merge names its target among its sources."""


class FormatOverrideExistsError(WorkCurationError):
    """The work already has an override for that format."""


class FormatOverrideNotFoundError(WorkCurationError):
    """The work has no override with that id."""


class FormatOverrideNotStoredError(CurationError):
    """An override just stored cannot be read back (not a refusal: the request fails)."""


class SongMasterNotFoundError(WorkCurationError):
    """The work has no song master."""


@dataclass(frozen=True)
class MergeResult:
    merged_file_count: int
    deleted_work_count: int
    dropped_override_count: int


@dataclass(frozen=True)
class SplitResult:
    new_work_id: str
    old_work_deleted: bool


@dataclass(frozen=True)
class ReassignResult:
    old_work_id: str | None
    old_work_deleted: bool


@dataclass(frozen=True)
class NewFormatOverride:
    """A format override to create on a work."""

    format_name: str
    preferred_file_id: UUID
    notes: str | None = None


@dataclass(frozen=True)
class MasterCandidate:
    """A present file of a work, with what the automatic master pick scores."""

    id: UUID
    release_status: str | None
    release_type: str | None
    format: str | None
    bitrate: int | None
    duration_ms: int | None


@dataclass
class SongMaster:
    id: UUID
    work_id: str
    preferred_file_id: UUID
    selection_method: SelectionMethod = SelectionMethod.AUTO
    score: int | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class FormatOverride:
    id: UUID
    work_id: str
    format_name: str
    preferred_file_id: UUID
    notes: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class PlayFileResolution:
    """Which file plays for one logged play: curation's view ``play_file_resolution`` (D17).

    ``file_id`` is the station-format override or the song master of the work, or ``None``
    when the play has neither (D22). ``file_status`` is that file's status as the view reports
    it; the consumer decides what is playable (D21). ``audio_silent`` is whether the final
    file's audio was measured silent (D111), as streaming stores it on the audio's cue
    row; false with no row. The M3U export leaves a silent play out (S7).
    """

    play_event_id: UUID
    file_id: UUID | None
    file_status: FileStatus | None
    audio_silent: bool = False

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

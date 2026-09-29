from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from backend.domain.enums import (
    AudioHashKind,
    EnrichmentStatus,
    FileStatus,
    ReleaseStatus,
    ReleaseType,
)


class LibraryError(Exception):
    """Base class for library-subdomain errors."""


class InvalidAudioHashError(LibraryError):
    """A value that is not a well-formed audio fingerprint."""


# Formats an audio fingerprint is defined for (spec B1). Other formats get none.
AUDIO_HASHABLE_FORMATS: frozenset[str] = frozenset({"flac", "mp3"})

_DIGEST_LENGTH: dict[str, int] = {
    AudioHashKind.FLAC_MD5: 32,
    AudioHashKind.AUDIO_SHA256: 64,
}
_LOWER_HEX = re.compile(r"[0-9a-f]+")


@dataclass(frozen=True)
class AudioHash:
    """A fingerprint of a file's audio alone: tags, cover art and padding do not change it.

    Stored as ``<kind>:<lowercase hex digest>``, e.g. ``flac-md5:0123…``.
    """

    kind: AudioHashKind
    digest: str

    def __post_init__(self) -> None:
        expected = _DIGEST_LENGTH.get(self.kind)
        well_formed = expected is not None and len(self.digest) == expected
        if not well_formed or _LOWER_HEX.fullmatch(self.digest) is None:
            raise InvalidAudioHashError(f"{self.kind}:{self.digest}")

    def __str__(self) -> str:
        return f"{self.kind}:{self.digest}"

    @classmethod
    def parse(cls, text: str) -> AudioHash:
        """The fingerprint stored as *text*; raises InvalidAudioHashError when malformed."""
        kind, sep, digest = text.partition(":")
        if not sep or kind not in _DIGEST_LENGTH:
            raise InvalidAudioHashError(text)
        return cls(AudioHashKind(kind), digest)


@dataclass
class AudioMetadata:
    """Metadata extracted from audio file tags."""

    recording_mbid: str | None = None
    artist_mbid: str | None = None
    album_artist_mbid: str | None = None
    release_mbid: str | None = None
    release_title: str | None = None
    release_type: ReleaseType | None = None
    release_type_secondary: str | None = None
    release_status: ReleaseStatus | None = None
    track_title: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    duration_ms: int | None = None
    bitrate: int | None = None
    artist_name: str | None = None
    normalized_artist_name: str | None = None
    normalized_title: str | None = None
    raw_metadata: dict[str, Any] | None = None


@dataclass
class LibraryFile:
    id: UUID
    file_path: str
    format: str
    enrichment_status: EnrichmentStatus = EnrichmentStatus.PENDING
    file_status: FileStatus = FileStatus.PRESENT
    indexed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    trace_id: str | None = None
    recording_id: str | None = None
    work_id: str | None = None
    # On-disk size and mtime at the moment the file was last read. An
    # incremental scan compares these against a fresh stat() and skips the
    # file unread when they match. None on rows indexed before these were
    # recorded; the scanner backfills them on its next visit.
    file_size: int | None = None
    file_mtime_ns: int | None = None
    # Fingerprint of the audio alone (see AudioHash). The scan fills a FLAC's
    # stored MD5; library_hash_backfill_task fills the rest. None until then,
    # and for formats that have none.
    audio_hash: AudioHash | None = None
    # When the file went missing: set by mark_missing, cleared whenever the row is
    # PRESENT again (the upsert, relocate). None while the file is on disk.
    missing_since: datetime | None = None
    audio: AudioMetadata = field(default_factory=AudioMetadata)


@dataclass
class LibraryQuarantine:
    id: UUID
    file_path: str
    error_message: str
    trace_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class LibraryFolder:
    id: UUID
    name: str
    full_path: str
    parent_id: UUID | None = None
    folder_hash: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class CaseDuplicateRepair:
    """Rows naming one file in different case, left by case-only renames.

    The keeper is the row spelled as the file is on disk; each stale row's
    references move to it and the stale row is deleted.
    """

    keeper_id: UUID
    keeper_path: str
    stale_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class MissingFileMove:
    """A MISSING row and the PRESENT row holding the same track (its successor).

    Applying it moves every reference to the missing row onto the successor
    and deletes the missing row.
    """

    missing_id: UUID
    missing_path: str
    missing_work_id: str | None
    successor_id: UUID
    successor_path: str
    successor_work_id: str | None

    @property
    def crosses_work(self) -> bool:
        return self.missing_work_id != self.successor_work_id


@dataclass(frozen=True)
class MissingFilePlan:
    """What reconciliation will do: moves, and the paths of rows it leaves alone.

    ``ambiguous``: several present copies and no way to tell which one.
    ``unmatched``: no present copy that is ready (grouped) to take over.
    """

    moves: tuple[MissingFileMove, ...]
    ambiguous: tuple[str, ...]
    unmatched: tuple[str, ...]


@dataclass(frozen=True)
class MissingFileReconciliation:
    """Counts from one reconciliation run.

    ``failed``: folds refused by the database, each rolled back on its own.
    ``masters_repicked``: works whose AUTO master sat on a missing file.
    """

    reconciled: int
    failed: int
    ambiguous: int
    unmatched: int
    masters_repicked: int


@dataclass(frozen=True)
class MissingFileRow:
    """One missing row as the Missing Files page lists it."""

    id: UUID
    file_path: str
    artist_name: str | None
    track_title: str | None
    release_title: str | None
    missing_since: datetime | None
    work_id: str | None
    work_title: str | None
    # Identity matches that still name this row: deleting it releases them.
    match_count: int
    # Whether the row's work still has a file on disk.
    work_has_present_file: bool


@dataclass(frozen=True)
class MissingFileListing:
    """A page of missing rows, with totals over every missing row."""

    rows: tuple[MissingFileRow, ...]
    total: int
    total_match_count: int


@dataclass(frozen=True)
class MissingFileCandidate:
    """A present file that could take over a missing row."""

    id: UUID
    file_path: str


@dataclass(frozen=True)
class MissingFileEntry:
    """A listed missing row and the present files reconciliation would accept for it."""

    row: MissingFileRow
    candidates: tuple[MissingFileCandidate, ...]


@dataclass(frozen=True)
class MissingFilePage:
    """A page of the Missing Files list, with totals over every missing row."""

    entries: tuple[MissingFileEntry, ...]
    total: int
    total_match_count: int


class MissingFileError(LibraryError):
    """Base class for errors about missing library rows."""


class InvalidMissingFileSelectionError(MissingFileError):
    """A deletion must name either some rows or every row, not both or neither."""


class MissingFileChangedError(MissingFileError):
    """A row read as missing was no longer missing when its delete ran.

    A concurrent scan restored it after its references were detached, so the
    caller must roll back everything the deletion did.
    """


@dataclass(frozen=True)
class MissingFileSelection:
    """Which missing rows to delete: these ids, or every missing row."""

    ids: tuple[UUID, ...] = ()
    every_row: bool = False

    def __post_init__(self) -> None:
        if self.every_row == bool(self.ids):
            raise InvalidMissingFileSelectionError("give either ids or every_row")


@dataclass(frozen=True)
class MissingFileDeletion:
    """What a deletion did.

    ``matches_released``: identity matches deleted with the rows; each identity
    left with no match went back to review. ``skipped``: ids no longer missing
    (restored or folded since they were listed) or unknown.
    """

    deleted: int
    matches_released: int
    skipped: int


@dataclass(frozen=True)
class MissingFilePurge:
    """What an after-scan purge deleted, and the unreplaced rows it held back.

    ``deleted``, ``matches_released`` and ``skipped`` as in MissingFileDeletion.
    ``awaiting_fingerprint``: rows with an audio fingerprint, kept while a present
    file still waits for one (the backfill may yet fold them). ``still_on_disk``: rows
    whose file exists after all, or whose check the disk refused (the walk missed
    them). ``unreadable_folder``: rows whose folder is there but could not be listed
    (the walk may have skipped them, not lost them).
    """

    deleted: int
    matches_released: int
    skipped: int
    awaiting_fingerprint: int
    still_on_disk: int
    unreadable_folder: int


class MissingFileNotFoundError(MissingFileError):
    """No missing row has this id (it came back, or was folded or deleted)."""


class RemapTargetNotFoundError(MissingFileError):
    """The chosen target file does not exist."""


class RemapTargetNotPresentError(MissingFileError):
    """The chosen target file is itself missing from disk."""


class RemapTargetUngroupedError(MissingFileError):
    """The chosen target has no work yet, so the moved matches would lose theirs."""


# User setting holding a PurgeMissingPolicy value.
PURGE_MISSING_SETTING = "library.purge_missing"

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from backend.domain.enums import EnrichmentStatus, FileStatus, ReleaseStatus, ReleaseType


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
    # SHA-256 of the file's content. None while a first scan's hashes are
    # still being filled in by library_hash_backfill_task.
    file_hash: str | None
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
    """Counts from one reconciliation run."""

    reconciled: int
    ambiguous: int
    unmatched: int

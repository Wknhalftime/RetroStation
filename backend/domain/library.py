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
    file_hash: str
    format: str
    enrichment_status: EnrichmentStatus = EnrichmentStatus.PENDING
    file_status: FileStatus = FileStatus.PRESENT
    indexed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    trace_id: str | None = None
    recording_id: str | None = None
    work_id: str | None = None
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

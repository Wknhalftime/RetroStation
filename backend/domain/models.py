from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from backend.domain.enums import (
    EnrichmentStatus,
    MatchStatus,
    MatchTier,
    ReleaseStatus,
    ReleaseType,
    SelectionMethod,
    TargetType,
    TaskStatus,
    TaskType,
    VersionType,
)


@dataclass
class Station:
    id: UUID
    call_letters: str
    name: str | None = None
    city: str | None = None
    format_name: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Playlist:
    id: UUID
    name: str
    content_hash: str
    ingested_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    station_id: UUID | None = None


@dataclass
class BroadcastDay:
    id: UUID
    station_id: UUID
    broadcast_date: date


@dataclass
class LogArtist:
    id: UUID
    original_name: str
    normalized_name: str
    match_status: MatchStatus = MatchStatus.PENDING
    artist_candidates: list[dict[str, Any]] | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    embedding: list[float] | None = None


@dataclass
class LogIdentity:
    id: UUID
    artist_id: UUID
    original_title: str
    normalized_title: str
    normalized_signature: str
    match_status: MatchStatus = MatchStatus.PENDING
    match_tier: MatchTier | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    embedding: list[float] | None = None


@dataclass
class LogEvent:
    id: UUID
    identity_id: UUID
    playlist_id: UUID
    played_at: datetime
    broadcast_day_id: UUID | None = None


@dataclass
class Artist:
    id: str  # MBID
    name: str
    sort_name: str
    disambiguation: str | None = None
    needs_enhancement: bool = True
    enhanced_at: datetime | None = None
    enhancement_error: str | None = None


@dataclass
class Work:
    id: str  # MBID
    title: str
    artist_id: str  # MBID
    needs_enhancement: bool = True
    enhanced_at: datetime | None = None
    enhancement_error: str | None = None
    embedding: list[float] | None = None


@dataclass
class Recording:
    id: str  # MBID
    title: str
    work_id: str | None = None
    duration_ms: int | None = None
    version_type: VersionType = VersionType.ORIGINAL
    needs_enhancement: bool = True
    enhanced_at: datetime | None = None
    enhancement_error: str | None = None
    embedding: list[float] | None = None


@dataclass
class LibraryFile:
    id: UUID
    file_path: str
    file_hash: str
    format: str
    enrichment_status: EnrichmentStatus = EnrichmentStatus.PENDING
    indexed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    trace_id: str | None = None
    recording_id: str | None = None
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
    raw_metadata: dict[str, Any] | None = None


@dataclass
class LibraryQuarantine:
    id: UUID
    file_path: str
    error_message: str
    trace_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Match:
    id: UUID
    confidence_score: float
    match_tier: MatchTier
    identity_id: UUID | None = None
    artist_id: UUID | None = None
    library_file_id: UUID | None = None
    target_id: str | None = None
    target_type: TargetType | None = None
    trace_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


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


@dataclass
class GlobalMappingRule:
    id: UUID
    source_pattern: str
    target_type: TargetType
    target_id: str
    priority: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class MbCache:
    id: UUID
    cache_key: str
    entity_type: str
    entity_mbid: str
    response_data: dict[str, Any]
    cached_at: datetime
    expires_at: datetime


@dataclass
class ProgressTracking:
    task_id: str
    task_type: TaskType
    status: TaskStatus
    progress_data: dict[str, Any]
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


@dataclass
class UserSetting:
    key: str
    value: str
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from backend.domain.enums import MatchStatus, MatchTier, ReasonCode


@dataclass
class BroadcastStation:
    id: UUID
    call_letters: str
    name: str | None = None
    city: str | None = None
    format_name: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class BroadcastPlaylist:
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
class BroadcastArtist:
    id: UUID
    original_name: str
    normalized_name: str
    match_status: MatchStatus = MatchStatus.PENDING
    artist_candidates: list[dict[str, Any]] | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    embedding: list[float] | None = None
    reason_code: ReasonCode | None = None
    reason_detail: str | None = None


@dataclass
class BroadcastTrackIdentity:
    id: UUID
    broadcast_artist_id: UUID
    original_title: str
    normalized_title: str
    normalized_signature: str
    match_status: MatchStatus = MatchStatus.PENDING
    match_tier: MatchTier | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    embedding: list[float] | None = None
    reason_code: ReasonCode | None = None
    reason_detail: str | None = None


@dataclass(frozen=True)
class BroadcastPlayEvent:
    id: UUID
    identity_id: UUID
    playlist_id: UUID
    played_at: datetime
    broadcast_day_id: UUID | None = None

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from backend.domain.enums import MatchStatus, MatchTier, ReasonCode
from backend.domain.system import StorageUnavailableError

_STATION_CHANGE_FIELDS = frozenset({"call_letters", "name", "city", "format_name"})


class BroadcastError(Exception):
    """Base class for broadcast (stations, playlists, logs) failures."""


class DuplicateCallLettersError(BroadcastError):
    """Another station already has these call letters, in any case (D72)."""


class UnknownStationError(BroadcastError):
    """No station has this id."""


class BroadcastStorageError(BroadcastError, StorageUnavailableError):
    """A broadcast repository lost its database connection mid-operation."""


@dataclass(frozen=True)
class StationChanges:
    """The fields a partial update sets; unset fields keep their values."""

    values: Mapping[str, str | None]

    def __post_init__(self) -> None:
        for key in self.values:
            if key not in _STATION_CHANGE_FIELDS:
                raise ValueError(f"StationChanges.{key} is not a field a station can change")
        if "call_letters" in self.values:
            call_letters = self.values["call_letters"]
            if not isinstance(call_letters, str) or call_letters == "":
                raise ValueError("StationChanges.call_letters must be a non-empty str")


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

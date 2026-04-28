from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from backend.domain.enums import MatchTier, TargetType


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
    work_id: str | None = None
    trace_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        has_identity = self.identity_id is not None
        has_artist = self.artist_id is not None
        if has_identity == has_artist:
            raise ValueError(
                "Match must have exactly one of identity_id or artist_id set; "
                f"got identity_id={self.identity_id!r}, artist_id={self.artist_id!r}"
            )


@dataclass
class MappingRule:
    """A system-wide pattern-matching override applied before tiered matching.

    Rules are evaluated against normalized_name (artist pipeline) and
    normalized_signature (identity pipeline) across all stations and playlists.
    Priority is descending — first match wins.

    No station-scoped or playlist-scoped rules exist. If they are introduced,
    this class should be renamed SystemMappingRule to provide a contrast point.
    """

    id: UUID
    source_pattern: str
    target_type: TargetType
    target_id: str
    priority: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

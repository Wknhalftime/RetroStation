from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from backend.domain.enums import MatchTier, TargetType
from backend.domain.system import StorageUnavailableError

if TYPE_CHECKING:
    from backend.domain.library import LibraryFile


class MatchingError(Exception):
    """Base class for matching-subdomain errors."""


class MatchingStorageError(MatchingError, StorageUnavailableError):
    """A matching repository lost its database connection mid-operation."""


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


@dataclass(frozen=True)
class CandidateExclusions:
    """Library files a curator rejected for one broadcast song, and their current works.

    A candidate is excluded when it is a rejected file or currently belongs to the same work as one
    (AUD-R022, D3): rejecting a suggestion rules out that work for that song only.
    """

    file_ids: frozenset[UUID] = frozenset()
    work_ids: frozenset[str] = frozenset()

    def allows(self, candidate: LibraryFile) -> bool:
        if candidate.id in self.file_ids:
            return False
        return not candidate.work_id or candidate.work_id not in self.work_ids


NO_EXCLUSIONS = CandidateExclusions()

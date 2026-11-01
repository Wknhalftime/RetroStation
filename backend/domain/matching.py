from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from backend.domain.enums import (
    CreditRole,
    LogNameLinkOrigin,
    LogNameLinkState,
    MatchStatus,
    MatchTier,
    TargetType,
)
from backend.domain.system import StorageUnavailableError

if TYPE_CHECKING:
    from backend.domain.library import LibraryFile


class MatchingError(Exception):
    """Base class for matching-subdomain errors."""


class MatchingStorageError(MatchingError, StorageUnavailableError):
    """A matching repository lost its database connection mid-operation."""


class RecheckNotQueuedError(MatchingError):
    """The task queue refused a matching re-check (the API answers 503)."""


class InvalidLogNameLinkError(MatchingError):
    """A log-name link the table would refuse: a strength outside 0 to 100, evidence without a
    version, or a negative credit part (C30)."""


class LogNameLinkTargetMissingError(MatchingError):
    """A log-name link to a log name or a catalog artist that does not exist (its foreign
    keys, C30)."""


class LogNameNotFoundError(MatchingError):
    """No log name (broadcast artist) has this id."""


class LogNameNotUnmatchableError(MatchingError):
    """Unmatch asked of a log name that is not matched or rejected: nothing to revert."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


class MusicBrainzUnavailableError(MatchingError):
    """Song matching's MusicBrainz recording search could not be answered: a transient failure
    (HTTP 429 or 5xx, a timeout, a network error) or this run's cut-off (AUD-R028, D16). The
    song is deferred, never decided on partial evidence."""


# Statuses a re-check rewinds to pending (AUD-R022 D1). An artist the curator rejected
# outright stays rejected; a song the curator rejected (manual_rejected) is re-checked on
# every run, without its rejected files (AUD-R029, spec 2026-10-13 R1, R2).
ARTIST_UNDECIDED_STATUSES: tuple[MatchStatus, ...] = (
    MatchStatus.NEEDS_REVIEW,
    MatchStatus.AUTO_REJECTED,
)
SONG_UNDECIDED_STATUSES: tuple[MatchStatus, ...] = (
    MatchStatus.NEEDS_REVIEW,
    MatchStatus.AUTO_REJECTED,
    MatchStatus.MANUAL_REJECTED,
)


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


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class LogNameLink:
    """A log name's link to one catalog artist (C30, AUD-R035).

    One per (name, artist) in every state, so a rejected pair stays remembered (C19).
    ``artist_id`` is the catalog artist's id; ``mbid`` is its MBID copied when the link was
    written, never read for matching (the artist's current MBID is). ``evidence`` is versioned
    JSON (C50: version 0 holds the old score and tier). ``credit_part``, ``role`` and
    ``join_after`` place the artist in the name's parsed credit (C44).
    """

    broadcast_artist_id: UUID
    artist_id: str
    state: LogNameLinkState
    origin: LogNameLinkOrigin
    strength: int
    evidence: Mapping[str, object]
    mbid: str | None = None
    credit_part: int = 0
    role: CreditRole = CreditRole.MAIN
    join_after: str | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        if not 0 <= self.strength <= 100:
            raise InvalidLogNameLinkError(f"strength {self.strength} is outside 0 to 100")
        if "version" not in self.evidence:
            raise InvalidLogNameLinkError("the evidence has no version")
        if self.credit_part < 0:
            raise InvalidLogNameLinkError(f"credit part {self.credit_part} is negative")


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

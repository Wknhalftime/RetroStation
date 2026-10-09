from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from backend.domain.enums import ArtistLinkOutcome, CatalogSource, VersionType
from backend.domain.system import StorageUnavailableError

# Exactly the spelling MusicBrainz's lookup endpoints accept: a hyphenated
# UUID, either case. Anything else (whitespace, braces, no hyphens) gets a
# 400 "Invalid mbid." rather than a 404.
_MBID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


class CatalogError(Exception):
    """Base class for catalog-subdomain errors."""


class InvalidMusicBrainzIdError(CatalogError):
    """A value that MusicBrainz would reject as a malformed MBID."""


class CatalogStorageError(CatalogError, StorageUnavailableError):
    """A catalog repository lost its database connection mid-operation."""


class InvalidLinkDecisionError(CatalogError):
    """A link decision whose outcome and candidate disagree (AUD-R026)."""


@dataclass(frozen=True)
class MusicBrainzId:
    """A well-formed MusicBrainz identifier (it may still not exist upstream)."""

    value: str

    def __post_init__(self) -> None:
        if _MBID_PATTERN.fullmatch(self.value) is None:
            raise InvalidMusicBrainzIdError(self.value)

    @classmethod
    def parse(cls, raw: str) -> MusicBrainzId | None:
        """Return the MBID for raw, or None when raw is malformed (e.g. a corrupt tag)."""
        if _MBID_PATTERN.fullmatch(raw) is None:
            return None
        return cls(raw)


@dataclass
class Artist:
    id: str  # local UUID (str); MusicBrainz ID lives in the separate `mbid` field
    name: str
    sort_name: str
    disambiguation: str | None = None
    needs_enhancement: bool = True
    enhanced_at: datetime | None = None
    enhancement_error: str | None = None
    mbid: str | None = None
    origin: CatalogSource = CatalogSource.LOCAL
    normalized_name: str | None = None
    # D15 (AUD-R026): when the local-artist linker last looked this artist up on MusicBrainz,
    # and what it decided. Both None until the first lookup.
    mb_lookup_at: datetime | None = None
    mb_lookup_outcome: ArtistLinkOutcome | None = None

    @property
    def linked_by_lookup(self) -> bool:
        """True when the local-artist linker gave this artist its MBID (AUD-R026)."""
        return self.mb_lookup_outcome == ArtistLinkOutcome.LINKED


@dataclass(frozen=True)
class ArtistLinkCandidate:
    """One MusicBrainz artist a local artist may be linked to (AUD-R026)."""

    mbid: str
    name: str
    sort_name: str
    disambiguation: str | None = None

    def __post_init__(self) -> None:
        MusicBrainzId(self.mbid)  # raises InvalidMusicBrainzIdError for a malformed MBID


@dataclass(frozen=True)
class LinkEvidence:
    """What the library says about one local artist (AUD-R026).

    ``tag_counts``: each distinct raw artist-MBID tag value of its present files, with how many
    present files carry it. One value may credit several artists ("a, b").
    """

    tag_counts: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class LinkDecision:
    """The linker's decision for one artist: a candidate exactly when the outcome is LINKED."""

    outcome: ArtistLinkOutcome
    candidate: ArtistLinkCandidate | None = None

    def __post_init__(self) -> None:
        if (self.outcome == ArtistLinkOutcome.LINKED) != (self.candidate is not None):
            raise InvalidLinkDecisionError(
                f"outcome {self.outcome.value} with candidate {self.candidate!r}"
            )


@dataclass
class Work:
    id: str  # local UUID (str); MusicBrainz ID lives in the separate `mbid` field
    title: str
    artist_id: str  # FK to Artist.id (local UUID, not an MBID)
    needs_enhancement: bool = True
    enhanced_at: datetime | None = None
    enhancement_error: str | None = None
    embedding: list[float] | None = None
    mbid: str | None = None
    origin: CatalogSource = CatalogSource.LOCAL


@dataclass(frozen=True)
class WorkFootprint:
    """A work plus how much references it — the input to duplicate planning."""

    id: str
    title: str
    artist_id: str
    file_count: int = 0
    match_count: int = 0


@dataclass(frozen=True)
class WorkMergePlan:
    """Fold ``source_ids`` into ``target_id``; all share an artist and title."""

    target_id: str
    source_ids: tuple[str, ...]


@dataclass
class Recording:
    id: str  # MusicBrainz recording MBID used directly as PK (no separate mbid field)
    title: str
    work_id: str | None = None
    duration_ms: int | None = None
    version_type: VersionType = VersionType.ORIGINAL
    needs_enhancement: bool = True
    enhanced_at: datetime | None = None
    enhancement_error: str | None = None
    embedding: list[float] | None = None

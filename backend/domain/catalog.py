from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from backend.domain.enums import CatalogSource, VersionType

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

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from backend.domain.enums import CatalogSource, VersionType


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

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

# MusicBrainz's special-purpose artists: placeholders, never a performer an artist can be. No
# link names one (C7, phase 1c S5), and the Library lists none (S7).
# Source: https://musicbrainz.org/doc/Style/Unknown_and_untitled/Special_purpose_artist
# (all twelve checked against it on 2026-10-08; the first four are also on the dev DB). The page
# gives no MBID for [christmas music], [classical music], [soundtrack], [nature sounds] or
# [news report]; the artist linker catches a bracketed MusicBrainz name by its brackets instead.
SPECIAL_PURPOSE_MBIDS: frozenset[str] = frozenset(
    {
        "89ad4ac3-39f7-470e-963a-56509c546377",  # Various Artists
        "125ec42a-7229-4250-afc5-e057484327fe",  # [unknown]
        "eec63d3c-3b81-4ad4-b1e4-7c147d4d2b61",  # [no artist]
        "314e1c25-dde7-4e4d-b2f4-0a7b9f7c56dc",  # [dialogue]
        "f731ccc4-e22a-43af-a747-64213329e088",  # [anonymous]
        "9be7f096-97ec-4615-8957-8d40b5dcbc41",  # [traditional]
        "33cf029c-63b0-41a0-9855-be2a3665fb3b",  # [data]
        "a0ef7e1d-44ff-4039-9435-7d5fefdeecc9",  # [theatre]
        "80a8851f-444c-4539-892b-ad2a49292aa9",  # [language instruction]
        "66ea0139-149f-4a0c-8fbf-5ea9ec4a6e49",  # [Disney]
        "90068d37-bae7-4292-be4a-704c145bd616",  # [church chimes]
        "7e84f845-ac16-41fe-9ff8-df12eb32af55",  # MusicBrainz Test Artist
    }
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
class RecordingVersion:
    """A recording's id and version type."""

    id: str
    version_type: VersionType


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

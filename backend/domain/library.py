from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from backend.domain.enums import (
    AudioHashKind,
    EnrichmentStatus,
    FileStatus,
    ReleaseStatus,
    ReleaseType,
)


class LibraryError(Exception):
    """Base class for library-subdomain errors."""


class InvalidAudioHashError(LibraryError):
    """A value that is not a well-formed audio fingerprint."""


# Formats an audio fingerprint is defined for (spec B1). Other formats get none.
AUDIO_HASHABLE_FORMATS: frozenset[str] = frozenset({"flac", "mp3"})

_DIGEST_LENGTH: dict[str, int] = {
    AudioHashKind.FLAC_MD5: 32,
    AudioHashKind.AUDIO_SHA256: 64,
}
_LOWER_HEX = re.compile(r"[0-9a-f]+")


@dataclass(frozen=True)
class AudioHash:
    """A fingerprint of a file's audio alone: tags, cover art and padding do not change it.

    Stored as ``<kind>:<lowercase hex digest>``, e.g. ``flac-md5:0123…``.
    """

    kind: AudioHashKind
    digest: str

    def __post_init__(self) -> None:
        expected = _DIGEST_LENGTH.get(self.kind)
        well_formed = expected is not None and len(self.digest) == expected
        if not well_formed or _LOWER_HEX.fullmatch(self.digest) is None:
            raise InvalidAudioHashError(f"{self.kind}:{self.digest}")

    def __str__(self) -> str:
        return f"{self.kind}:{self.digest}"

    @classmethod
    def parse(cls, text: str) -> AudioHash:
        """The fingerprint stored as *text*; raises InvalidAudioHashError when malformed."""
        kind, sep, digest = text.partition(":")
        if not sep or kind not in _DIGEST_LENGTH:
            raise InvalidAudioHashError(text)
        return cls(AudioHashKind(kind), digest)


@dataclass
class AudioMetadata:
    """Metadata extracted from audio file tags."""

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
    artist_name: str | None = None
    normalized_artist_name: str | None = None
    normalized_title: str | None = None
    raw_metadata: dict[str, Any] | None = None


@dataclass
class LibraryFile:
    id: UUID
    file_path: str
    format: str
    enrichment_status: EnrichmentStatus = EnrichmentStatus.PENDING
    file_status: FileStatus = FileStatus.PRESENT
    indexed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    trace_id: str | None = None
    recording_id: str | None = None
    work_id: str | None = None
    # On-disk size and mtime at the moment the file was last read. An
    # incremental scan compares these against a fresh stat() and skips the
    # file unread when they match. None on rows indexed before these were
    # recorded; the scanner backfills them on its next visit.
    file_size: int | None = None
    file_mtime_ns: int | None = None
    # Fingerprint of the audio alone (see AudioHash). The scan fills a FLAC's
    # stored MD5; library_hash_backfill_task fills the rest. None until then,
    # and for formats that have none.
    audio_hash: AudioHash | None = None
    audio: AudioMetadata = field(default_factory=AudioMetadata)


@dataclass
class LibraryQuarantine:
    id: UUID
    file_path: str
    error_message: str
    trace_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class LibraryFolder:
    id: UUID
    name: str
    full_path: str
    parent_id: UUID | None = None
    folder_hash: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class CaseDuplicateRepair:
    """Rows naming one file in different case, left by case-only renames.

    The keeper is the row spelled as the file is on disk; each stale row's
    references move to it and the stale row is deleted.
    """

    keeper_id: UUID
    keeper_path: str
    stale_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class MissingFileMove:
    """A MISSING row and the PRESENT row holding the same track (its successor).

    Applying it moves every reference to the missing row onto the successor
    and deletes the missing row.
    """

    missing_id: UUID
    missing_path: str
    missing_work_id: str | None
    successor_id: UUID
    successor_path: str
    successor_work_id: str | None

    @property
    def crosses_work(self) -> bool:
        return self.missing_work_id != self.successor_work_id


@dataclass(frozen=True)
class MissingFilePlan:
    """What reconciliation will do: moves, and the paths of rows it leaves alone.

    ``ambiguous``: several present copies and no way to tell which one.
    ``unmatched``: no present copy that is ready (grouped) to take over.
    """

    moves: tuple[MissingFileMove, ...]
    ambiguous: tuple[str, ...]
    unmatched: tuple[str, ...]


@dataclass(frozen=True)
class MissingFileReconciliation:
    """Counts from one reconciliation run.

    ``failed``: folds refused by the database, each rolled back on its own.
    ``masters_repicked``: works whose AUTO master sat on a missing file.
    """

    reconciled: int
    failed: int
    ambiguous: int
    unmatched: int
    masters_repicked: int

from __future__ import annotations

import csv
import hashlib
import io
import posixpath
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import chardet
import structlog

from backend.domain.broadcast import (
    BroadcastArtist,
    BroadcastDay,
    BroadcastPlayEvent,
    BroadcastPlaylist,
    BroadcastTrackIdentity,
)
from backend.repositories.broadcast_artists import BroadcastArtistRepository
from backend.repositories.broadcast_days import BroadcastDayRepository
from backend.repositories.broadcast_play_events import BroadcastPlayEventRepository
from backend.repositories.broadcast_playlists import BroadcastPlaylistRepository
from backend.repositories.broadcast_track_identities import BroadcastTrackIdentityRepository
from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)

logger = structlog.get_logger()

# Minimum chardet confidence before we trust a non-UTF-8 detection. chardet
# often reports "ascii" at confidence 1.0 for files that contain multi-byte
# UTF-8 it simply didn't sample, so the ascii branch is rejected separately.
_MIN_CHARDET_CONFIDENCE = 0.7

# Every Nth valid row, `ingest_csv` fires the progress callback. Trade-off
# between autocommit write cost and UI responsiveness.
_PROGRESS_REPORT_INTERVAL = 100


def _is_valid_ingest_row(row: Mapping[str, str]) -> bool:
    """Single source of truth for 'is this CSV row ingestible?'.

    Used by both ``count_csv_rows`` and the ``ingest_csv`` row loop so the
    pre-count total and the committed count cannot drift silently.
    """
    return bool(
        row.get("Artist", "").strip()
        and row.get("Title", "").strip()
        and row.get("Played", "").strip()
    )


class IngestionError(Exception):
    """Base class for ingestion-layer failures surfaced to callers."""


class CsvDecodeError(IngestionError):
    """Raised when a CSV file's bytes cannot be decoded to text."""


class DuplicatePlaylistError(IngestionError):
    """Raised when a CSV with the same content hash has already been ingested."""


def _decode_csv_bytes(file_bytes: bytes) -> str:
    """Decode CSV bytes via a strict ladder; never silently mangle characters.

    Ladder: UTF-8 with BOM, UTF-8 strict, then chardet only when it reports a
    non-ascii encoding with reasonable confidence. We deliberately do not fall
    back to latin-1, because latin-1 accepts any byte sequence and would turn
    mis-encoded UTF-8 into mojibake instead of surfacing the problem.
    """
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    detected = chardet.detect(file_bytes)
    detected_encoding = (detected.get("encoding") or "").lower()
    confidence = detected.get("confidence") or 0.0
    if (
        detected_encoding
        and detected_encoding != "ascii"
        and confidence >= _MIN_CHARDET_CONFIDENCE
    ):
        try:
            text = file_bytes.decode(detected_encoding)
        except (UnicodeDecodeError, LookupError):
            pass
        else:
            logger.info(
                "csv_decoded_non_utf8",
                encoding=detected_encoding,
                confidence=confidence,
            )
            # chardet-labelled UTF-16-LE/BE leave the BOM in the decoded
            # string, which would poison the first CSV header key. Strip it.
            return text.lstrip("\ufeff")
    raise CsvDecodeError(
        "Unable to decode CSV file as UTF-8 and no confident alternate encoding "
        "was detected; re-export the file as UTF-8."
    )


def count_csv_rows(file_bytes: bytes) -> int:
    """Return the number of valid (ingestible) rows in a CSV payload.

    Shares :func:`_decode_csv_bytes` with ``ingest_csv`` so encoding
    handling is identical. Propagates :class:`CsvDecodeError` — callers
    (the task layer) treat a decode failure as a terminal FAILED state
    rather than silently degrading to a zero total.
    """
    text = _decode_csv_bytes(file_bytes)
    reader = csv.DictReader(io.StringIO(text))
    return sum(1 for row in reader if _is_valid_ingest_row(row))


@dataclass
class IngestionResult:
    playlist_id: str = ""
    rows_processed: int = 0
    artists_created: int = 0
    identities_created: int = 0
    events_created: int = 0
    broadcast_days_created: int = 0


def ingest_csv(
    file_bytes: bytes,
    file_name: str,
    station_id: str,
    playlist_repo: BroadcastPlaylistRepository,
    broadcast_artist_repo: BroadcastArtistRepository,
    track_identity_repo: BroadcastTrackIdentityRepository,
    play_event_repo: BroadcastPlayEventRepository,
    broadcast_day_repo: BroadcastDayRepository,
    *,
    on_row_processed: Callable[[int], None] | None = None,
) -> IngestionResult:
    """Parse a CSV file and create playlist, artists, identities, events, and broadcast days.

    ``on_row_processed`` receives the cumulative count of valid rows
    processed so far. It is called at three kinds of moments:

    1. **Attempt-zero**: exactly once, with ``0``, immediately before the
       row loop. This resets any caller-side ``last_processed`` bookkeeping
       when ``@retry_on_deadlock`` re-enters ``ingest_csv``.
    2. **Cadence**: every ``_PROGRESS_REPORT_INTERVAL`` valid rows.
    3. **Final**: once after the loop with the terminal count, **only
       when the last cadence tick did not already land on that count**.
       This keeps a caller tracking progress synchronized with
       ``result.rows_processed`` without emitting a duplicate value when
       ``rows_processed`` is 0 or an exact multiple of the interval.

    Implementations MUST treat ``0`` as a valid value and be idempotent
    with respect to rewound counts during a deadlock retry.
    """
    result = IngestionResult()

    text = _decode_csv_bytes(file_bytes)

    # Content hash for deduplication
    content_hash = hashlib.sha256(file_bytes).hexdigest()

    # Check for duplicate
    existing = playlist_repo.get_by_content_hash(content_hash)
    if existing is not None:
        raise DuplicatePlaylistError(
            f"CSV already ingested as playlist {existing.id}"
        )

    # Create playlist
    playlist = playlist_repo.create(BroadcastPlaylist(
        id=uuid4(),
        name=posixpath.basename(file_name),
        content_hash=content_hash,
        station_id=UUID(station_id) if station_id else None,
    ))
    result.playlist_id = str(playlist.id)

    # Parse CSV
    reader = csv.DictReader(io.StringIO(text))

    seen_artists: dict[str, BroadcastArtist] = {}  # normalized_name → BroadcastArtist
    seen_signatures: dict[str, BroadcastTrackIdentity] = {}  # signature → BroadcastTrackIdentity
    seen_broadcast_dates: dict[str, BroadcastDay] = {}  # date_str → BroadcastDay

    if on_row_processed is not None:
        on_row_processed(0)

    for row in reader:
        if not _is_valid_ingest_row(row):
            continue
        raw_artist = row.get("Artist", "").strip()
        raw_title = row.get("Title", "").strip()
        played_str = row.get("Played", "").strip()

        # Parse played_at
        played_at = datetime.strptime(played_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)

        # Normalize
        norm_artist = normalize_artist(raw_artist)
        norm_title = normalize_title(raw_title)
        signature = compute_normalized_signature(norm_artist, norm_title)

        # Upsert artist
        if norm_artist not in seen_artists:
            input_id = uuid4()
            stored = broadcast_artist_repo.upsert(BroadcastArtist(
                id=input_id,
                original_name=raw_artist,
                normalized_name=norm_artist,
            ))
            seen_artists[norm_artist] = stored
            if stored.id == input_id:
                result.artists_created += 1
        artist = seen_artists[norm_artist]

        # Upsert identity
        if signature not in seen_signatures:
            identity_input_id = uuid4()
            identity = track_identity_repo.upsert(BroadcastTrackIdentity(
                id=identity_input_id,
                broadcast_artist_id=artist.id,
                original_title=raw_title,
                normalized_title=norm_title,
                normalized_signature=signature,
            ))
            seen_signatures[signature] = identity
            if identity.id == identity_input_id:
                result.identities_created += 1
        identity = seen_signatures[signature]

        # Broadcast day
        date_str = played_at.date().isoformat()
        if date_str not in seen_broadcast_dates and station_id:
            seen_broadcast_dates[date_str] = broadcast_day_repo.get_or_create(
                UUID(station_id), played_at.date()
            )
            result.broadcast_days_created += 1
        broadcast_day = seen_broadcast_dates.get(date_str)

        # Create event
        play_event_repo.create(BroadcastPlayEvent(
            id=uuid4(),
            identity_id=identity.id,
            playlist_id=playlist.id,
            played_at=played_at,
            broadcast_day_id=broadcast_day.id if broadcast_day else None,
        ))
        result.events_created += 1
        result.rows_processed += 1

        if (
            on_row_processed is not None
            and result.rows_processed % _PROGRESS_REPORT_INTERVAL == 0
        ):
            on_row_processed(result.rows_processed)

    # Final fire: only when the last cadence tick did NOT already land on
    # result.rows_processed. This includes 0 (no loop iterations, but the
    # attempt-zero signal already sent 0) and any exact multiple of the
    # interval. Prevents a duplicate callback emit.
    if (
        on_row_processed is not None
        and result.rows_processed % _PROGRESS_REPORT_INTERVAL != 0
    ):
        on_row_processed(result.rows_processed)

    logger.info(
        "ingestion_complete",
        playlist_id=result.playlist_id,
        rows=result.rows_processed,
        artists=result.artists_created,
        identities=result.identities_created,
        events=result.events_created,
    )
    return result

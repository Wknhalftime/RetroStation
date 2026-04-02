from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime
from uuid import uuid4

import chardet
import structlog

from backend.domain.models import (
    BroadcastDay,
    LogArtist,
    LogEvent,
    LogIdentity,
    Playlist,
)
from backend.repositories.broadcast_days import BroadcastDayRepository
from backend.repositories.log_artists import LogArtistRepository
from backend.repositories.log_events import LogEventRepository
from backend.repositories.log_identities import LogIdentityRepository
from backend.repositories.playlists import PlaylistRepository
from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)

logger = structlog.get_logger()


class IngestionResult:
    def __init__(self) -> None:
        self.playlist_id = ""
        self.rows_processed = 0
        self.artists_created = 0
        self.identities_created = 0
        self.events_created = 0
        self.broadcast_days_created = 0


def ingest_csv(
    file_bytes: bytes,
    file_name: str,
    station_id: str,
    playlist_repo: PlaylistRepository,
    log_artist_repo: LogArtistRepository,
    log_identity_repo: LogIdentityRepository,
    log_event_repo: LogEventRepository,
    broadcast_day_repo: BroadcastDayRepository,
) -> IngestionResult:
    """Parse a CSV file and create playlist, artists, identities, events, and broadcast days."""
    result = IngestionResult()

    # Detect encoding
    detected = chardet.detect(file_bytes)
    encoding = detected.get("encoding") or "utf-8"
    text = file_bytes.decode(encoding)

    # Content hash for deduplication
    content_hash = hashlib.sha256(file_bytes).hexdigest()

    # Check for duplicate
    existing = playlist_repo.get_by_content_hash(content_hash)
    if existing is not None:
        raise ValueError(f"CSV already ingested as playlist {existing.id}")

    # Create playlist
    from uuid import UUID
    playlist = playlist_repo.create(Playlist(
        id=uuid4(),
        name=file_name,
        content_hash=content_hash,
        station_id=UUID(station_id) if station_id else None,
    ))
    result.playlist_id = str(playlist.id)

    # Parse CSV
    reader = csv.DictReader(io.StringIO(text))

    seen_artists: dict[str, LogArtist] = {}  # normalized_name → LogArtist
    seen_signatures: dict[str, LogIdentity] = {}  # signature → LogIdentity
    seen_broadcast_dates: dict[str, BroadcastDay] = {}  # date_str → BroadcastDay

    for row in reader:
        raw_artist = row.get("Artist", "").strip()
        raw_title = row.get("Title", "").strip()
        played_str = row.get("Played", "").strip()

        if not raw_artist or not raw_title or not played_str:
            continue

        # Parse played_at
        played_at = datetime.strptime(played_str, "%Y-%m-%d %H:%M:%S")

        # Normalize
        norm_artist = normalize_artist(raw_artist)
        norm_title = normalize_title(raw_title)
        signature = compute_normalized_signature(norm_artist, norm_title)

        # Upsert artist
        if norm_artist not in seen_artists:
            artist = log_artist_repo.upsert(LogArtist(
                id=uuid4(),
                original_name=raw_artist,
                normalized_name=norm_artist,
            ))
            seen_artists[norm_artist] = artist
            if artist.id == artist.id:  # always true, but tracks creation
                result.artists_created += 1
        artist = seen_artists[norm_artist]

        # Upsert identity
        if signature not in seen_signatures:
            identity = log_identity_repo.upsert(LogIdentity(
                id=uuid4(),
                artist_id=artist.id,
                original_title=raw_title,
                normalized_title=norm_title,
                normalized_signature=signature,
            ))
            seen_signatures[signature] = identity
            result.identities_created += 1
        identity = seen_signatures[signature]

        # Broadcast day
        date_str = played_at.date().isoformat()
        if date_str not in seen_broadcast_dates and station_id:
            bd = broadcast_day_repo.get_or_create(
                UUID(station_id), played_at.date()
            )
            seen_broadcast_dates[date_str] = bd
            result.broadcast_days_created += 1
        broadcast_day = seen_broadcast_dates.get(date_str)

        # Create event
        log_event_repo.create(LogEvent(
            id=uuid4(),
            identity_id=identity.id,
            playlist_id=playlist.id,
            played_at=played_at,
            broadcast_day_id=broadcast_day.id if broadcast_day else None,
        ))
        result.events_created += 1
        result.rows_processed += 1

    logger.info(
        "ingestion_complete",
        playlist_id=result.playlist_id,
        rows=result.rows_processed,
        artists=result.artists_created,
        identities=result.identities_created,
        events=result.events_created,
    )
    return result

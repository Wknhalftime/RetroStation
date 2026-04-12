from __future__ import annotations

import csv
import hashlib
import io
import posixpath
from datetime import UTC, datetime
from uuid import uuid4

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
from backend.repositories.play_events import BroadcastPlayEventRepository
from backend.repositories.playlists import BroadcastPlaylistRepository
from backend.repositories.track_identities import BroadcastTrackIdentityRepository
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
    playlist_repo: BroadcastPlaylistRepository,
    broadcast_artist_repo: BroadcastArtistRepository,
    track_identity_repo: BroadcastTrackIdentityRepository,
    play_event_repo: BroadcastPlayEventRepository,
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

    for row in reader:
        raw_artist = row.get("Artist", "").strip()
        raw_title = row.get("Title", "").strip()
        played_str = row.get("Played", "").strip()

        if not raw_artist or not raw_title or not played_str:
            continue

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
            bd = broadcast_day_repo.get_or_create(
                UUID(station_id), played_at.date()
            )
            seen_broadcast_dates[date_str] = bd
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

    logger.info(
        "ingestion_complete",
        playlist_id=result.playlist_id,
        rows=result.rows_processed,
        artists=result.artists_created,
        identities=result.identities_created,
        events=result.events_created,
    )
    return result
